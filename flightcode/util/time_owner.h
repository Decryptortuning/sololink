#ifndef OPENSOLO_TIME_OWNER_H
#define OPENSOLO_TIME_OWNER_H
#include <errno.h>
#include <unistd.h>
#ifndef OPENSOLO_NETWORK_TIME_MARKER
#define OPENSOLO_NETWORK_TIME_MARKER "/run/opensolo-network-time.active"
#endif
/* The SysV time service removes this marker only after its daemon stops.
 * Fail closed on unexpected filesystem errors: never race a network owner. */
static inline int opensolo_gps_may_step(void)
{
    int saved = errno;
    int allowed = access(OPENSOLO_NETWORK_TIME_MARKER, F_OK) < 0 && errno == ENOENT;
    errno = saved;
    return allowed;
}
#endif
