#include "iso_vcd.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

struct IsoVcd {
    FILE *fp;
    uint64_t pending_time;
    int time_written;
    int in_dumpvars;
    int n;
    int cap;
    uint64_t *last;
    int *width;
    unsigned char *valid;
    char **idstr;
};

static char *iso_dup (const char *s)
{
    size_t n;
    char *p;

    n = strlen(s) + 1;
    p = malloc(n);
    if (p == NULL) {
        return NULL;
    }
    memcpy(p, s, n);
    return p;
}

static int iso_grow (IsoVcd *v)
{
    int cap;
    uint64_t *last;
    int *width;
    unsigned char *valid;
    char **idstr;

    cap = v->cap == 0 ? 64 : v->cap * 2;
    last = realloc(v->last, (size_t) cap * sizeof(*last));
    width = realloc(v->width, (size_t) cap * sizeof(*width));
    valid = realloc(v->valid, (size_t) cap * sizeof(*valid));
    idstr = realloc(v->idstr, (size_t) cap * sizeof(*idstr));
    if (last == NULL || width == NULL || valid == NULL || idstr == NULL) {
        return 0;
    }
    v->last = last;
    v->width = width;
    v->valid = valid;
    v->idstr = idstr;
    v->cap = cap;
    return 1;
}

static void iso_vcd_ident (char *buf, int id)
{
    /* Printable VCD identifier: s0, s1, ... */
    sprintf(buf, "s%d", id);
}

static void iso_write_bits (FILE *fp, uint64_t value, int width)
{
    int i;

    if (width <= 1) {
        fputc((value & 1ULL) ? '1' : '0', fp);
        return;
    }
    fputc('b', fp);
    for (i = width - 1; i >= 0; i--) {
        fputc(((value >> i) & 1ULL) ? '1' : '0', fp);
    }
}

IsoVcd *iso_vcd_open (const char *path, const char *timescale)
{
    IsoVcd *v;

    v = calloc(1, sizeof(*v));
    if (v == NULL) {
        return NULL;
    }
    v->fp = fopen(path, "w");
    if (v->fp == NULL) {
        free(v);
        return NULL;
    }
    if (timescale == NULL || timescale[0] == '\0') {
        timescale = "1 ns";
    }
    fprintf(v->fp, "$date\n    isomorph\n$end\n");
    fprintf(v->fp, "$version\n    isomorph c99\n$end\n");
    fprintf(v->fp, "$timescale\n    %s\n$end\n", timescale);
    return v;
}

void iso_vcd_close (IsoVcd *v)
{
    int i;

    if (v == NULL) {
        return;
    }
    if (v->fp != NULL) {
        fclose(v->fp);
    }
    if (v->idstr != NULL) {
        for (i = 0; i < v->n; i++) {
            free(v->idstr[i]);
        }
    }
    free(v->last);
    free(v->width);
    free(v->valid);
    free(v->idstr);
    free(v);
}

void iso_vcd_push_scope (IsoVcd *v, const char *name)
{
    if (v == NULL || v->fp == NULL || name == NULL) {
        return;
    }
    fprintf(v->fp, "$scope module %s $end\n", name);
}

void iso_vcd_pop_scope (IsoVcd *v)
{
    if (v == NULL || v->fp == NULL) {
        return;
    }
    fprintf(v->fp, "$upscope $end\n");
}

int iso_vcd_wire (IsoVcd *v, const char *name, int width)
{
    char buf[32];
    int id;

    if (v == NULL || name == NULL) {
        return -1;
    }
    if (width < 1) {
        width = 1;
    }
    if (v->n >= v->cap && !iso_grow(v)) {
        return -1;
    }
    id = v->n;
    iso_vcd_ident(buf, id);
    v->idstr[id] = iso_dup(buf);
    v->width[id] = width;
    v->last[id] = 0;
    v->valid[id] = 0;
    v->n++;
    if (v->fp != NULL) {
        fprintf(v->fp, "$var wire %d %s %s $end\n", width, buf, name);
    }
    return id;
}

void iso_vcd_finish_defs (IsoVcd *v)
{
    if (v == NULL || v->fp == NULL) {
        return;
    }
    fprintf(v->fp, "$enddefinitions $end\n");
    fprintf(v->fp, "$dumpvars\n");
    v->in_dumpvars = 1;
}

void iso_vcd_end_dumpvars (IsoVcd *v)
{
    if (v == NULL || v->fp == NULL) {
        return;
    }
    fprintf(v->fp, "$end\n");
    v->in_dumpvars = 0;
}

void iso_vcd_time (IsoVcd *v, uint64_t t)
{
    if (v == NULL) {
        return;
    }
    v->pending_time = t;
    v->time_written = 0;
}

void iso_vcd_change (IsoVcd *v, int id, uint64_t value, int width)
{
    if (v == NULL || v->fp == NULL || id < 0 || id >= v->n) {
        return;
    }
    if (width < 1) {
        width = v->width[id];
    }
    if (v->valid[id] && v->last[id] == value && !v->in_dumpvars) {
        return;
    }
    if (!v->in_dumpvars && !v->time_written) {
        fprintf(v->fp, "#%llu\n", (unsigned long long) v->pending_time);
        v->time_written = 1;
    }
    iso_write_bits(v->fp, value, width);
    if (width > 1) {
        fputc(' ', v->fp);
    }
    fputs(v->idstr[id], v->fp);
    fputc('\n', v->fp);
    v->last[id] = value;
    v->valid[id] = 1;
}
