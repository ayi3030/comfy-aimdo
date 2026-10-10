#include "plat.h"
#include "xfer-file.h"

#define HOSTBUF_FILE_READER_WINDOW (64ULL * 1024ULL * 1024ULL)
#define LEAD_IN_THRESHOLD (HOSTBUF_FILE_READER_WINDOW - 16ULL * 1024ULL * 1024ULL)

static bool hostbuf_file_reader_retire_active(void) {
    HostbufFileReaderSlot *slot;

    if (g_devctx->_hostbuf_file_reader_active < 0) {
        return true;
    }

    slot = &g_devctx->_hostbuf_file_reader_slots[g_devctx->_hostbuf_file_reader_active];
    if (!slot->offset) {
        return true;
    }
    if (slot->event) {
        log(AIMDO_LOG_ERROR, "%s: active slot %d already has a completion event\n", __func__,
            g_devctx->_hostbuf_file_reader_active);
        return false;
    }
    return CHECK_CU(cuEventCreate(&slot->event, GPU_EVENT_DISABLE_TIMING)) &&
           CHECK_CU(cuEventRecord(slot->event, (gpu_stream_t)slot->stream));
}

static HostbufFileReaderSlot *hostbuf_file_reader_next(gpu_stream_t stream) {
    HostbufFileReaderSlot *slot;

    g_devctx->_hostbuf_file_reader_active =
        (g_devctx->_hostbuf_file_reader_active + 1) % HOSTBUF_FILE_READER_SLOTS;
    slot = &g_devctx->_hostbuf_file_reader_slots[g_devctx->_hostbuf_file_reader_active];

    if (slot->buffer && slot->event) {
        if (!CHECK_CU(cuEventSynchronize(slot->event)) ||
            !CHECK_CU(cuEventDestroy(slot->event))) {
            return NULL;
        }
        slot->event = NULL;
    }

    if (!slot->buffer &&
        !CHECK_CU_OOM_ERROR(cuMemAllocHost((void **)&slot->buffer, HOSTBUF_FILE_READER_WINDOW))) {
        return NULL;
    }

    slot->offset = 0;
    slot->stream = (gpu_stream_t)stream;
    return slot;
}

SHARED_EXPORT
bool hostbuf_file_reader_read(int device, uint64_t file_handle, uint64_t file_offset,
                              uint64_t size, gpu_stream_t stream,
                              uint64_t device_ptr, bool mark_cold) {
    if (size == 0) {
        return true;
    }
    if (!device_ptr || device < 0 || !set_devctx_for_device(device)) {
        log(AIMDO_LOG_ERROR, "%s: input validation failed device_ptr=%p device=%d\n",
            __func__, (void *)(uintptr_t)device_ptr, device);
        return false;
    }

    while (size) {
        HostbufFileReaderSlot *slot = g_devctx->_hostbuf_file_reader_active < 0 ? NULL :
            &g_devctx->_hostbuf_file_reader_slots[g_devctx->_hostbuf_file_reader_active];
        size_t chunk;

        if (!slot || slot->stream != (gpu_stream_t)stream ||
            (slot->offset + size >= HOSTBUF_FILE_READER_WINDOW &&
             slot->offset >= LEAD_IN_THRESHOLD)) {
            if (!hostbuf_file_reader_retire_active() ||
                !(slot = hostbuf_file_reader_next(stream))) {
                return false;
            }
        }

        chunk = (size_t)MIN(size, HOSTBUF_FILE_READER_WINDOW - slot->offset);
        if (!xfer_file_read(file_handle, file_offset, slot->buffer + slot->offset,
                            chunk, mark_cold)) {
            log(AIMDO_LOG_ERROR, "%s: file read failed handle=0x%llx offset=%llu size=%zu\n",
                __func__, (ull)file_handle, (ull)file_offset, chunk);
            return false;
        }
        gpu_result_t copy_result = cuMemcpyHtoDAsync((gpu_deviceptr_t)device_ptr,
                                                 slot->buffer + slot->offset,
                                                 chunk, (gpu_stream_t)stream);
        if (!CHECK_CU(copy_result)) {
            log(AIMDO_LOG_ERROR, "%s: device copy failed result=%d device_ptr=%p device=%d stream=%p size=%zu\n",
                __func__, (int)copy_result, (void *)(uintptr_t)device_ptr, device,
                (void *)stream, chunk);
            return false;
        }

        slot->offset += chunk;
        file_offset += chunk;
        device_ptr += chunk;
        size -= chunk;
    }

    return true;
}

SHARED_EXPORT
void hostbuf_file_reader_cleanup(void) {
    if (!g_devctx) {
        return;
    }

    hostbuf_file_reader_retire_active();
    for (unsigned i = 0; i < HOSTBUF_FILE_READER_SLOTS; i++) {
        HostbufFileReaderSlot *slot = &g_devctx->_hostbuf_file_reader_slots[i];

        if (slot->buffer && slot->event) {
            CHECK_CU(cuEventSynchronize(slot->event));
            CHECK_CU(cuEventDestroy(slot->event));
        }
        if (slot->buffer) {
            CHECK_CU(cuMemFreeHost(slot->buffer));
        }
    }
    memset(g_devctx->_hostbuf_file_reader_slots, 0,
           sizeof(g_devctx->_hostbuf_file_reader_slots));
    g_devctx->_hostbuf_file_reader_active = -1;
}
