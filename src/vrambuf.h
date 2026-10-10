#pragma once

#include "plat.h"

typedef struct VramBuffer {
    gpu_deviceptr_t base_ptr;
    size_t max_size;
    size_t allocated;
    size_t handle_count;
    int device;
    struct VramBuffer *next;
    gpu_mem_handle_t handles[1];
} VramBuffer;

SHARED_EXPORT
void *vrambuf_create(int device, size_t max_size);

SHARED_EXPORT
bool vrambuf_grow(void *arg, size_t required_size);

SHARED_EXPORT
bool vrambuf_destroy(void *arg);

SHARED_EXPORT
gpu_deviceptr_t vrambuf_get(void *arg);
