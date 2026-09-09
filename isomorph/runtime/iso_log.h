#ifndef ISO_LOG_H
#define ISO_LOG_H

#include <stdint.h>

typedef struct IsoLog IsoLog;

IsoLog *iso_log_open(const char *path);
void iso_log_close(IsoLog *l);
void iso_log_begin(IsoLog *l, uint64_t cycle, const char *clock);
void iso_log_field(IsoLog *l, const char *name, uint64_t value);
void iso_log_end(IsoLog *l);

#endif /* ISO_LOG_H */
