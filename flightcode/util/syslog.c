/* OpenSolo nonblocking logger. Never interpose libc's fortified logging API. */
#include "opensolo_log.h"
#include <errno.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <time.h>
#include <unistd.h>
#ifndef SYSLOG_SOCK_NAME
#define SYSLOG_SOCK_NAME "/dev/log"
#endif
#ifndef SYSLOG_MSG_MAX
#define SYSLOG_MSG_MAX 512
#endif
static int log_fd = -1;
static const char *log_ident = "-";
static int log_facility = LOG_USER;
static int log_options;
static unsigned log_dropped;
static int log_mask = 0xff;

static int connect_logger(void)
{
    struct sockaddr_un address;
    memset(&address, 0, sizeof(address));
    address.sun_family = AF_UNIX;
    if (strlen(SYSLOG_SOCK_NAME) >= sizeof(address.sun_path))
        return -1;
    memcpy(address.sun_path, SYSLOG_SOCK_NAME, sizeof(SYSLOG_SOCK_NAME));
    return connect(log_fd, (struct sockaddr *)&address, sizeof(address));
}
void opensolo_closelog(void)
{
    if (log_fd >= 0)
        close(log_fd);
    log_fd = -1;
    log_ident = "-";
    log_facility = LOG_USER;
    log_options = 0;
    __atomic_store_n(&log_dropped, 0, __ATOMIC_RELAXED);
}
void opensolo_openlog(const char *ident, int options, int facility)
{
    int saved = errno;
    opensolo_closelog();
    log_ident = ident ? ident : "-";
    log_facility = facility & LOG_FACMASK;
    log_options = options;
    log_fd = socket(AF_UNIX, SOCK_DGRAM | SOCK_NONBLOCK | SOCK_CLOEXEC, 0);
    /* Keep the nonblocking socket even if syslogd is temporarily absent. */
    if (log_fd >= 0)
        (void)connect_logger();
    errno = saved;
}
int opensolo_setlogmask(int mask)
{
    if (mask)
        return __atomic_exchange_n(&log_mask, mask & 0xff, __ATOMIC_RELAXED);
    return __atomic_load_n(&log_mask, __ATOMIC_RELAXED);
}
static int emit(int priority, const char *format, va_list args)
{
    if (log_fd < 0 || priority < 0 || priority > 7 || !format)
        return -1;
    if (!(LOG_MASK(priority) & __atomic_load_n(&log_mask, __ATOMIC_RELAXED)))
        return 0;
    struct timespec now;
    struct tm tm;
    char timestamp[32], pid[32] = "", message[SYSLOG_MSG_MAX];
    if (clock_gettime(CLOCK_REALTIME, &now) || !localtime_r(&now.tv_sec, &tm))
        return -1;
    if (!strftime(timestamp, sizeof(timestamp), "%b %e %T", &tm))
        return -1;
    if (log_options & LOG_PID)
        snprintf(pid, sizeof(pid), "[%ld]", (long)getpid());
    int n = snprintf(message, sizeof(message), "<%d>%s +%03ld %s%s: ",
                     log_facility | priority, timestamp, now.tv_nsec / 1000000,
                     log_ident, pid);
    if (n < 0)
        return -1;
    size_t length = (size_t)n < sizeof(message) ? (size_t)n : sizeof(message) - 1;
    n = vsnprintf(message + length, sizeof(message) - length, format, args);
    if (n < 0)
        return -1;
    size_t room = sizeof(message) - length;
    length += (size_t)n < room ? (size_t)n : room - 1;
    ssize_t sent = send(log_fd, message, length, MSG_DONTWAIT | MSG_NOSIGNAL);
    /* A replaced /dev/log socket needs a fresh connection; never wait/retry
     * a full receiver queue. At most one reconnect/send attempt per message. */
    if (sent < 0 && (errno == ENOTCONN || errno == EDESTADDRREQ || errno == ECONNREFUSED || errno == EPIPE)) {
        if (!connect_logger())
            sent = send(log_fd, message, length, MSG_DONTWAIT | MSG_NOSIGNAL);
    }
    return sent == (ssize_t)length ? 0 : -1;
}
static int emit_format(int priority, const char *format, ...)
{
    va_list args;
    va_start(args, format);
    int result = emit(priority, format, args);
    va_end(args);
    return result;
}
void opensolo_vsyslog(int priority, const char *format, va_list args)
{
    int saved = errno;
    unsigned dropped = __atomic_exchange_n(&log_dropped, 0, __ATOMIC_RELAXED);
    if (dropped && emit_format(LOG_ERR, "dropped %u messages", dropped))
        __atomic_fetch_add(&log_dropped, dropped, __ATOMIC_RELAXED);
    if (emit(priority, format, args))
        __atomic_fetch_add(&log_dropped, 1, __ATOMIC_RELAXED);
    errno = saved;
}
void opensolo_syslog(int priority, const char *format, ...)
{
    va_list args;
    va_start(args, format);
    opensolo_vsyslog(priority, format, args);
    va_end(args);
}
