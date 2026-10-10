#pragma once

#include "vmm-ref.h"

#include <stdbool.h>

typedef enum {
    ROGUE_HANDOFF_ERROR,
    ROGUE_HANDOFF_FREED,
    ROGUE_HANDOFF_OWNED,
} RogueHandoff;

bool register_rogue_candidate(gpu_deviceptr_t ptr);
void unregister_rogue_candidate(gpu_deviceptr_t ptr);
bool rogue_candidate_freed(gpu_deviceptr_t ptr);
RogueHandoff handoff_rogue(VirtualRange *range, gpu_deviceptr_t ptr,
                           PhysicalPage **pages, size_t page_count);
bool rogue_exists(gpu_deviceptr_t ptr);
bool free_rogue(gpu_deviceptr_t ptr, int *result);
