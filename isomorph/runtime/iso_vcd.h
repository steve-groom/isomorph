#ifndef ISO_VCD_H
#define ISO_VCD_H

#include <stdint.h>

typedef struct IsoVcd IsoVcd;

IsoVcd *iso_vcd_open(const char *path, const char *timescale);
void iso_vcd_close(IsoVcd *v);
void iso_vcd_push_scope(IsoVcd *v, const char *name);
void iso_vcd_pop_scope(IsoVcd *v);
int iso_vcd_wire(IsoVcd *v, const char *name, int width);
void iso_vcd_finish_defs(IsoVcd *v);
void iso_vcd_end_dumpvars(IsoVcd *v);
void iso_vcd_time(IsoVcd *v, uint64_t t);
void iso_vcd_change(IsoVcd *v, int id, uint64_t value, int width);

#endif /* ISO_VCD_H */
