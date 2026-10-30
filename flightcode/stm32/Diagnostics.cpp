#include "Diagnostics.h"
#include "packetTypes.h"
#include "SLIP.h"
#include <sys/socket.h>
#include <sys/select.h>
#include <unistd.h>
#include <string.h>
#include <time.h>
static uint64_t nowNs() {
    timespec ts;
    if (clock_gettime(CLOCK_MONOTONIC, &ts)) return 0;
    return uint64_t(ts.tv_sec) * 1000000000ULL + ts.tv_nsec;
}
Diagnostics::Diagnostics(unsigned port) : fd(-1), peer(), nextRequest(0),
    deadline(0), token(0), pending(false) {
    pthread_mutex_init(&mutex, NULL);
    if (port > 65535) return;
    fd = socket(AF_INET, SOCK_DGRAM | SOCK_NONBLOCK | SOCK_CLOEXEC, 0);
    if (fd < 0) return;
    sockaddr_in address = {};
    address.sin_family = AF_INET;
    address.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    address.sin_port = htons(port);
    if (fd >= FD_SETSIZE || bind(fd, reinterpret_cast<sockaddr*>(&address), sizeof(address))) {
        close(fd); fd = -1;
    }
}
Diagnostics::~Diagnostics() {
    if (fd >= 0) close(fd);
    pthread_mutex_destroy(&mutex);
}
void Diagnostics::downHandler(int serial) {
    unsigned char request[4]; // Extra byte detects oversized datagrams.
    sockaddr_in sender = {};
    socklen_t size = sizeof(sender);
    int n = recvfrom(fd, request, sizeof(request), MSG_DONTWAIT | MSG_TRUNC,
                     reinterpret_cast<sockaddr*>(&sender), &size);
    if (n != 3 || request[0] != 1 || sender.sin_addr.s_addr != htonl(INADDR_LOOPBACK)) return;
    uint64_t now = nowNs();
    pthread_mutex_lock(&mutex);
    if (!now || now < nextRequest) { pthread_mutex_unlock(&mutex); return; }
    nextRequest = now + 1000000000ULL;
    deadline = nextRequest;
    token = uint16_t(request[1]) | (uint16_t(request[2]) << 8);
    peer = sender;
    pending = true;
    pthread_mutex_unlock(&mutex);
    char raw[4] = {PKT_ID_DIAGNOSTICS, 1, char(request[1]), char(request[2])};
    char encoded[10];
    SLIPEncoder slip(encoded, sizeof(encoded));
    int len = slip.encode(raw, sizeof(raw));
    // Same sole downstream writer as other packets. Never replay a short write.
    if (len < 0 || write(serial, encoded, len) != len) {
        pthread_mutex_lock(&mutex); pending = false; pthread_mutex_unlock(&mutex);
    }
}
int Diagnostics::upHandler(const char *data, int len) {
    const unsigned char *p = reinterpret_cast<const unsigned char*>(data);
    if (len != 204 || p[0] != 1 || (p[1] & ~3) || p[2] != 204 || p[3] || p[6] || p[7]) return 0;
    uint64_t now = nowNs();
    pthread_mutex_lock(&mutex);
    if (!pending || !now || now >= deadline || token != (unsigned(p[4]) | (unsigned(p[5]) << 8))) {
        pthread_mutex_unlock(&mutex); return 0;
    }
    sockaddr_in destination = peer;
    pending = false;
    pthread_mutex_unlock(&mutex);
    int sent = sendto(fd, data, len, MSG_DONTWAIT, reinterpret_cast<sockaddr*>(&destination), sizeof(destination));
    return sent > 0 ? sent : 0;
}
