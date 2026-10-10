#pragma once

#include "gpu_abi.h"

#include <stddef.h>

typedef struct VirtualRange {
    gpu_deviceptr_t address;
    size_t bytes;
    size_t refs;
} VirtualRange;

typedef struct PhysicalAllocation {
    gpu_mem_handle_t handle;
    size_t bytes;
    size_t refs;
    struct PhysicalPage *references;
} PhysicalAllocation;

typedef struct PhysicalPage {
    PhysicalAllocation *allocation;
    gpu_deviceptr_t address;
    struct PhysicalPage *next;
} PhysicalPage;

VirtualRange *virtual_range_alloc(size_t bytes, size_t alignment);
VirtualRange *virtual_range_ref(VirtualRange *range);
gpu_result_t virtual_range_unref(VirtualRange *range);
gpu_result_t physical_page_alloc(PhysicalPage **page, size_t bytes, int device);
PhysicalPage *physical_page_ref(PhysicalPage *page, gpu_deviceptr_t address);
gpu_result_t physical_page_unref(PhysicalPage *page);

static inline gpu_deviceptr_t virtual_range_get(VirtualRange *range) {
    return range->address;
}

static inline gpu_mem_handle_t physical_page_get(PhysicalPage *page) {
    return page->allocation->handle;
}
