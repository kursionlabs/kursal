#include <arpa/inet.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <uv.h>

typedef struct kur_tcp_server kur_tcp_server;
typedef struct kur_tcp_client kur_tcp_client;

typedef struct {
    uv_write_t request;
    kur_tcp_client *client;
    char *bytes;
    uv_buf_t buffer;
} kur_tcp_write;

struct kur_tcp_client {
    uv_tcp_t handle; /* first: uv callbacks can recover the enclosing owner */
    kur_tcp_server *server;
    unsigned pending_writes;
    int eof;
    int closing;
};

struct kur_tcp_server {
    uv_loop_t loop;
    uv_tcp_t listener;
    int32_t (*handler)(uint8_t *context, uint8_t *client, uint8_t *bytes, size_t len);
    uint8_t *context;
    int32_t status;
    uint16_t port;
};

static void client_closed(uv_handle_t *handle) {
    free((kur_tcp_client *)handle);
}

static void close_client_when_drained(kur_tcp_client *client) {
    if (client->eof && client->pending_writes == 0 && !client->closing) {
        client->closing = 1;
        uv_close((uv_handle_t *)&client->handle, client_closed);
    }
}

static void stop_client(kur_tcp_client *client, int32_t error) {
    if (error < 0 && client->server->status == 0) client->server->status = error;
    uv_read_stop((uv_stream_t *)&client->handle);
    client->eof = 1;
    close_client_when_drained(client);
}

static void write_done(uv_write_t *request, int status) {
    kur_tcp_write *write = (kur_tcp_write *)request;
    kur_tcp_client *client = write->client;
    free(write->bytes);
    free(write);
    client->pending_writes--;
    if (status < 0 && !client->eof) {
        stop_client(client, status);
    } else {
        if (status < 0 && client->server->status == 0) client->server->status = status;
        close_client_when_drained(client);
    }
}

/* Copies the bytes before returning. Kursal may free its Buffer! immediately. */
int32_t kur_uv_tcp_write_copy(uint8_t *raw_client, uint8_t *bytes, size_t len) {
    kur_tcp_client *client = (kur_tcp_client *)raw_client;
    if (len > UINT32_MAX) return UV_EINVAL;
    kur_tcp_write *write = malloc(sizeof *write);
    if (!write) return UV_ENOMEM;
    write->bytes = malloc(len ? len : 1);
    if (!write->bytes) {
        free(write);
        return UV_ENOMEM;
    }
    memcpy(write->bytes, bytes, len);
    write->client = client;
    write->buffer = uv_buf_init(write->bytes, (unsigned int)len);
    int status = uv_write(&write->request, (uv_stream_t *)&client->handle, &write->buffer, 1, write_done);
    if (status < 0) {
        free(write->bytes);
        free(write);
        return status;
    }
    client->pending_writes++;
    return 0;
}

static void allocate_read(uv_handle_t *handle, size_t suggested, uv_buf_t *buffer) {
    (void)handle;
    size_t size = suggested ? suggested : 4096;
    buffer->base = malloc(size);
    buffer->len = buffer->base ? size : 0;
}

static void read_done(uv_stream_t *stream, ssize_t nread, const uv_buf_t *buffer) {
    kur_tcp_client *client = (kur_tcp_client *)stream;
    if (nread > 0) {
        /* 0: keep reading, 1: close after queued writes, negative: error. */
        int32_t status = client->server->handler(client->server->context,
            (uint8_t *)client, (uint8_t *)buffer->base, (size_t)nread);
        if (status == 1) stop_client(client, 0);
        else if (status < 0) stop_client(client, status);
    } else if (nread < 0) {
        stop_client(client, nread == UV_EOF ? 0 : (int32_t)nread);
    }
    /* nread == 0 is EAGAIN-like: no data and no state change. */
    free(buffer->base);
}

static void listener_closed(uv_handle_t *handle) { (void)handle; }

static void connection_ready(uv_stream_t *listener, int status) {
    kur_tcp_server *server = (kur_tcp_server *)listener->data;
    if (status < 0) {
        server->status = status;
        uv_close((uv_handle_t *)&server->listener, listener_closed);
        return;
    }
    kur_tcp_client *client = calloc(1, sizeof *client);
    if (!client) {
        server->status = UV_ENOMEM;
        uv_close((uv_handle_t *)&server->listener, listener_closed);
        return;
    }
    client->server = server;
    status = uv_tcp_init(&server->loop, &client->handle);
    if (status < 0) {
        server->status = status;
        free(client);
        uv_close((uv_handle_t *)&server->listener, listener_closed);
        return;
    }
    status = uv_accept(listener, (uv_stream_t *)&client->handle);
    uv_close((uv_handle_t *)&server->listener, listener_closed); /* one client only */
    if (status < 0) {
        server->status = status;
        uv_close((uv_handle_t *)&client->handle, client_closed);
        return;
    }
    status = uv_read_start((uv_stream_t *)&client->handle, allocate_read, read_done);
    if (status < 0) stop_client(client, status);
}

/* Returns NULL on bind/listen failure; context remains caller-owned. */
uint8_t *kur_uv_tcp_new(uint16_t port,
    int32_t (*handler)(uint8_t *, uint8_t *, uint8_t *, size_t), uint8_t *context,
    int32_t *out_error) {
    *out_error = 0;
    kur_tcp_server *server = calloc(1, sizeof *server);
    if (!server) { *out_error = UV_ENOMEM; return NULL; }
    int status = uv_loop_init(&server->loop);
    if (status < 0) { *out_error = status; free(server); return NULL; }
    status = uv_tcp_init(&server->loop, &server->listener);
    if (status < 0) {
        *out_error = status;
        if (uv_loop_close(&server->loop) != 0) abort();
        free(server);
        return NULL;
    }
    server->handler = handler;
    server->context = context;
    server->listener.data = server;
    struct sockaddr_in address;
    status = uv_ip4_addr("127.0.0.1", port, &address);
    if (status == 0) status = uv_tcp_bind(&server->listener, (const struct sockaddr *)&address, 0);
    if (status == 0) status = uv_listen((uv_stream_t *)&server->listener, 1, connection_ready);
    if (status == 0) {
        int size = sizeof address;
        status = uv_tcp_getsockname(&server->listener, (struct sockaddr *)&address, &size);
        if (status == 0) server->port = ntohs(address.sin_port);
    }
    if (status < 0) {
        *out_error = status;
        uv_close((uv_handle_t *)&server->listener, listener_closed);
        if (uv_run(&server->loop, UV_RUN_DEFAULT) != 0) abort();
        if (uv_loop_close(&server->loop) != 0) abort();
        free(server);
        return NULL;
    }
    return (uint8_t *)server;
}

uint16_t kur_uv_tcp_port(uint8_t *raw_server) {
    kur_tcp_server *server = (kur_tcp_server *)raw_server;
    return server->port;
}

/* Before run(), no connection callback has executed, so only the listener exists. */
void kur_uv_tcp_stop_and_free(uint8_t *raw_server) {
    kur_tcp_server *server = (kur_tcp_server *)raw_server;
    uv_close((uv_handle_t *)&server->listener, listener_closed);
    if (uv_run(&server->loop, UV_RUN_DEFAULT) != 0) abort();
    if (uv_loop_close(&server->loop) != 0) abort();
    free(server);
}

/* Terminal operation: every handle must close before the loop is freed. */
int32_t kur_uv_tcp_run_and_free(uint8_t *raw_server) {
    kur_tcp_server *server = (kur_tcp_server *)raw_server;
    int active = uv_run(&server->loop, UV_RUN_DEFAULT);
    if (active != 0) abort();
    int status = uv_loop_close(&server->loop);
    if (status != 0) abort();
    status = server->status;
    free(server);
    return status;
}
