#ifndef OPENSOLO_DIAGNOSTICS_H
#define OPENSOLO_DIAGNOSTICS_H
#include <stdint.h>
#include <pthread.h>
#include <netinet/in.h>
// Read-only bridge endpoint. No separate UART open or ownership.
class Diagnostics {
public:
    explicit Diagnostics(unsigned port);
    ~Diagnostics();
    int getfd() const { return fd; }
    void downHandler(int serial);
    int upHandler(const char *data, int len);
private:
    Diagnostics(const Diagnostics &);
    Diagnostics &operator=(const Diagnostics &);
    int fd;
    pthread_mutex_t mutex;
    sockaddr_in peer;
    uint64_t nextRequest, deadline;
    uint16_t token;
    bool pending;
};
#endif
