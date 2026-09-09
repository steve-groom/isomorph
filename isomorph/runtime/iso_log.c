#include "iso_log.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

struct IsoLog {
    FILE *fp;
    uint64_t cycle;
    char clock[64];
    int n;
    int cap;
    int used;
    char **name;
    uint64_t *last;
    unsigned char *valid;
    unsigned char *changed;
    uint64_t *value;
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

static int iso_log_grow (IsoLog *l)
{
    int cap;
    char **name;
    uint64_t *last;
    unsigned char *valid;
    unsigned char *changed;
    uint64_t *value;

    cap = l->cap == 0 ? 64 : l->cap * 2;
    name = realloc(l->name, (size_t) cap * sizeof(*name));
    last = realloc(l->last, (size_t) cap * sizeof(*last));
    valid = realloc(l->valid, (size_t) cap * sizeof(*valid));
    changed = realloc(l->changed, (size_t) cap * sizeof(*changed));
    value = realloc(l->value, (size_t) cap * sizeof(*value));
    if (name == NULL || last == NULL || valid == NULL
            || changed == NULL || value == NULL) {
        return 0;
    }
    l->name = name;
    l->last = last;
    l->valid = valid;
    l->changed = changed;
    l->value = value;
    l->cap = cap;
    return 1;
}

IsoLog *iso_log_open (const char *path)
{
    IsoLog *l;

    l = calloc(1, sizeof(*l));
    if (l == NULL) {
        return NULL;
    }
    l->fp = fopen(path, "w");
    if (l->fp == NULL) {
        free(l);
        return NULL;
    }
    return l;
}

void iso_log_close (IsoLog *l)
{
    int i;

    if (l == NULL) {
        return;
    }
    if (l->fp != NULL) {
        fclose(l->fp);
    }
    if (l->name != NULL) {
        for (i = 0; i < l->n; i++) {
            free(l->name[i]);
        }
    }
    free(l->name);
    free(l->last);
    free(l->valid);
    free(l->changed);
    free(l->value);
    free(l);
}

void iso_log_begin (IsoLog *l, uint64_t cycle, const char *clock)
{
    int i;

    if (l == NULL) {
        return;
    }
    l->cycle = cycle;
    l->clock[0] = '\0';
    if (clock != NULL) {
        strncpy(l->clock, clock, sizeof l->clock - 1);
        l->clock[sizeof l->clock - 1] = '\0';
    }
    l->used = 0;
    for (i = 0; i < l->n; i++) {
        l->changed[i] = 0;
    }
}

void iso_log_field (IsoLog *l, const char *name, uint64_t value)
{
    int i;

    if (l == NULL || name == NULL) {
        return;
    }
    i = l->used;
    if (i < l->n && l->name[i] != NULL && strcmp(l->name[i], name) == 0) {
        /* stable call order: slot i is this field */
    } else {
        if (l->n >= l->cap && !iso_log_grow(l)) {
            return;
        }
        i = l->n;
        l->name[i] = iso_dup(name);
        l->last[i] = 0;
        l->valid[i] = 0;
        l->n++;
    }
    l->used = i + 1;
    if (l->valid[i] && l->last[i] == value) {
        l->changed[i] = 0;
        l->value[i] = value;
        return;
    }
    l->changed[i] = 1;
    l->value[i] = value;
}

void iso_log_end (IsoLog *l)
{
    int i;
    int first;

    if (l == NULL || l->fp == NULL) {
        return;
    }
    first = 1;
    fprintf(l->fp, "{\"t\": %llu, \"cycle\": %llu",
            (unsigned long long) l->cycle,
            (unsigned long long) l->cycle);
    if (l->clock[0] != '\0') {
        fprintf(l->fp, ", \"clock\": \"%s\"", l->clock);
    }
    fprintf(l->fp, ", \"ch\": {");
    for (i = 0; i < l->used; i++) {
        if (!l->changed[i]) {
            continue;
        }
        if (!first) {
            fputc(',', l->fp);
        }
        first = 0;
        fprintf(l->fp, "\"%s\": %llu", l->name[i],
                (unsigned long long) l->value[i]);
        l->last[i] = l->value[i];
        l->valid[i] = 1;
    }
    fprintf(l->fp, "}}\n");
}
