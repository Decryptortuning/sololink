#ifndef OPENSOLO_LOG_H
#define OPENSOLO_LOG_H
#include <stdarg.h>
#include <syslog.h>
#ifdef __cplusplus
extern "C" {
#endif
/* Initialize before starting workers; close only after joining them.
 * Workers share a nonblocking datagram socket and atomic mask/drop counters.
 * Facility is selected here. LOG_PID is supported; LOG_PERROR/LOG_CONS are
 * intentionally not used: stderr/console output could block control threads.
 * Ident must remain valid until close, as with libc openlog. */
void opensolo_openlog(const char *ident, int options, int facility);
void opensolo_closelog(void);
int opensolo_setlogmask(int mask);
void opensolo_syslog(int priority, const char *format, ...)
    __attribute__((format(printf, 2, 3)));
void opensolo_vsyslog(int priority, const char *format, va_list args)
    __attribute__((format(printf, 2, 0)));
#ifdef __cplusplus
}
#endif
#endif
