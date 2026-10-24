/* WS53 SFC v150 command-only model. Register layout: SDK 163d9150.
 * The NOR peer, timings, protection and reset rules are EXPLICIT ASSUMPTIONS,
 * not GD25LE32E silicon/NV/cache/DMA qualification. No SSI wire timing or IRQ.
 * SPDX-License-Identifier: GPL-2.0-or-later
 */
#include "qemu/bswap.h"
#include "qemu/cutils.h"
#define TYPE_WS53_SFC_COMMAND "ws53-sfc-command"
OBJECT_DECLARE_SIMPLE_TYPE(WS53SfcCommandState, WS53_SFC_COMMAND)
#define WS53_SFC_NOR_SIZE (4u * 1024 * 1024)
#define WS53_SFC_CMD_NS 10000
#define WS53_SFC_PROGRAM_NS 200000
#define WS53_SFC_ERASE_NS 1000000
#define WS53_SFC_STATUS_NS 100000
#define WS53_SFC_CONFIG_MASK 0x000e7ffbu

struct WS53SfcCommandState {
    SysBusDevice parent_obj;
    MemoryRegion mmio, xip_rejected, clock_observation;
    QEMUTimer *command_timer, *peer_timer;
    uint8_t *array;
    uint32_t config, instruction, address;
    uint8_t data[64];
    uint8_t command_opcode, command_data[64];
    uint32_t command_address, command_length;
    uint8_t peer_opcode, peer_data[64];
    uint32_t peer_address, peer_length;
    bool assume_profile, controller_stall, peer_stall, power_cycle_level;
    bool configured_handoff;
    char *snapshot_global, *snapshot_dma, *snapshot_clock;
    char *snapshot_status3;
    bool has_status3;
    uint8_t status3;
    uint32_t initial_global, initial_dma, initial_clock;
    uint32_t global_config, dma_control, clock_state;
    bool peer_busy, peer_wel, volatile_enable, peer_nonvolatile;
    uint8_t sr1_config, sr1_live, sr2, nv_sr1, nv_sr2;
    int64_t command_remaining, peer_remaining;
    uint64_t commands_started, commands_completed, rejections;
    uint64_t mutating_commands_started;
    uint64_t peer_operations, peer_commits, power_cycles;
};

static void ws53_sfc_update_status(WS53SfcCommandState *s)
{
    s->sr1_live = s->sr1_config | (s->peer_wel ? 2 : 0) |
                  (s->peer_busy ? 1 : 0);
}

static MemTxResult ws53_sfc_reject(WS53SfcCommandState *s, const char *operation,
                                 hwaddr off, uint64_t value, unsigned size,
                                 const char *reason)
{
    s->rejections++;
    qemu_log_mask(LOG_GUEST_ERROR,
        "ws53-sfc-command: rejected %s +0x%" HWADDR_PRIx
        " value=0x%" PRIx64 " size=%u reason=%s\n",
        operation, off, value, size, reason);
    return MEMTX_ERROR;
}

static void ws53_sfc_pause(QEMUTimer *timer, int64_t *remaining)
{
    if (timer_pending(timer)) {
        *remaining = MAX(timer_expire_time_ns(timer) -
                         qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL), 1);
        timer_del(timer);
    }
}

static void ws53_sfc_resume_peer(WS53SfcCommandState *s)
{
    if (s->peer_busy && !s->peer_stall && !timer_pending(s->peer_timer)) {
        timer_mod(s->peer_timer, qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) +
                  s->peer_remaining);
    }
}

static void ws53_sfc_resume_command(WS53SfcCommandState *s)
{
    if ((s->config & 1) && !s->controller_stall &&
        !timer_pending(s->command_timer)) {
        timer_mod(s->command_timer, qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) +
                  s->command_remaining);
    }
}

static void ws53_sfc_peer_complete(void *opaque)
{
    WS53SfcCommandState *s = opaque;
    switch (s->peer_opcode) {
    case 0x02:
        for (uint32_t i = 0; i < s->peer_length; i++) {
            s->array[s->peer_address + i] &= s->peer_data[i];
        }
        break;
    case 0x20:
        memset(s->array + s->peer_address, 0xff, 4096);
        break;
    case 0x01:
        s->sr1_config = s->peer_data[0];
        s->sr2 = s->peer_data[1];
        if (s->peer_nonvolatile) {
            s->nv_sr1 = s->sr1_config;
            s->nv_sr2 = s->sr2;
        }
        break;
    default:
        g_assert_not_reached();
    }
    s->peer_busy = false;
    s->peer_commits++;
    ws53_sfc_update_status(s);
}

static void ws53_sfc_command_complete(void *opaque)
{
    WS53SfcCommandState *s = opaque;
    switch (s->command_opcode) {
    case 0x9f:
        /* Deliberately synthetic ID, never the observed board's 16:60:c8. */
        s->data[0] = 0x7f; s->data[1] = 0x53; s->data[2] = 0x01;
        break;
    case 0x05: s->data[0] = s->sr1_live; break;
    case 0x35: s->data[0] = s->sr2; break;
    case 0x15: s->data[0] = s->status3; break;
    case 0x06: s->peer_wel = true; s->volatile_enable = false; break;
    case 0x04: s->peer_wel = false; s->volatile_enable = false; break;
    case 0x50: s->volatile_enable = true; s->peer_wel = false; break;
    case 0x03:
        memcpy(s->data, s->array + s->command_address, s->command_length);
        break;
    case 0x02:
    case 0x20:
    case 0x01:
        s->peer_opcode = s->command_opcode;
        s->peer_address = s->command_address;
        s->peer_length = s->command_length;
        memcpy(s->peer_data, s->command_data, sizeof(s->peer_data));
        s->peer_nonvolatile = !s->volatile_enable;
        s->peer_busy = true;
        s->peer_wel = s->volatile_enable = false;
        s->peer_remaining = s->peer_opcode == 0x02 ? WS53_SFC_PROGRAM_NS :
                            s->peer_opcode == 0x20 ? WS53_SFC_ERASE_NS :
                            WS53_SFC_STATUS_NS;
        s->peer_operations++;
        ws53_sfc_resume_peer(s);
        break;
    default:
        g_assert_not_reached();
    }
    s->config &= ~1u;
    s->commands_completed++;
    ws53_sfc_update_status(s);
}

/* Validate completely before changing any command, latch, data or peer state. */
static const char *ws53_sfc_validate(WS53SfcCommandState *s, uint32_t config)
{
    uint8_t op = s->instruction;
    bool read = config & (1u << 8), data = config & (1u << 7);
    bool address = config & 8;
    uint32_t length = ((config >> 9) & 63) + 1;
    if (s->configured_handoff) {
        /* Admission checks, NOT proof of physical clock, ownership or drain.
         * DMA is not modeled: a supplied busy indication cannot self-clear. */
        unsigned source = (s->clock_state >> 1) & 7;
        if (s->dma_control & 1) { return "snapshot-dma-busy-unqualified"; }
        if (!(s->clock_state & 1)) { return "snapshot-clock-disabled"; }
        if (source != 0 && source != 6) { return "snapshot-clock-source-unsupported"; }
        if (s->global_config & 1) { return "snapshot-spi-mode-unsupported"; }
        if (s->global_config & 4) { return "snapshot-address-mode-unsupported"; }
    }
    if (!(config & 2)) { return "unsupported-chip-select"; }
    if (config & ((7u << 17) | (7u << 4))) {
        return "unsupported-interface-or-dummy";
    }
    if (s->peer_busy && op != 0x05 && op != 0x35 &&
        !(op == 0x15 && s->has_status3)) {
        return "nor-busy";
    }
    switch (op) {
    case 0x9f:
        if (!read || !data || address || length != 3) {
            return "invalid-jedec-shape";
        }
        return NULL;
    case 0x05:
    case 0x35:
        if (!read || !data || address || length != 1) {
            return "invalid-status-read-shape";
        }
        return NULL;
    case 0x15:
        if (!s->has_status3) { return "unsupported-opcode"; }
        if (!read || !data || address || length != 1) {
            return "invalid-status-read-shape";
        }
        return NULL;
    case 0x06:
    case 0x04:
    case 0x50:
        if (read || data || address) { return "invalid-enable-shape"; }
        return NULL;
    case 0x01:
        if (read || !data || address || length != 2) {
            return "invalid-status-write-shape";
        }
        if (!s->peer_wel && !s->volatile_enable) {
            return "status-write-not-enabled";
        }
        /* Synthetic contract: BP!=0 protects all bytes, QE is only stored.
         * No SRP/lock/CMP/real device protection region semantics are implied. */
        if ((s->data[0] & ~0x1c) || (s->data[1] & ~2)) {
            return "unsupported-status-bits";
        }
        return NULL;
    case 0x03:
    case 0x02:
        if (read != (op == 0x03) || !data || !address) {
            return "invalid-array-transfer-shape";
        }
        if (s->address >= WS53_SFC_NOR_SIZE ||
            length > WS53_SFC_NOR_SIZE - s->address) {
            return "array-out-of-range";
        }
        if (op == 0x03) { return NULL; }
        if ((s->address & 255) + length > 256) {
            return "program-crosses-page";
        }
        break;
    case 0x20:
        if (read || data || !address) { return "invalid-erase-shape"; }
        if (s->address >= WS53_SFC_NOR_SIZE) { return "array-out-of-range"; }
        if (s->address & 4095) { return "erase-not-aligned"; }
        break;
    default:
        return "unsupported-opcode";
    }
    if (!s->peer_wel) { return "array-write-not-enabled"; }
    if (s->sr1_config & 0x1c) { return "synthetic-array-protected"; }
    return NULL;
}

static MemTxResult ws53_sfc_read(void *opaque, hwaddr off, uint64_t *value,
                                unsigned size, MemTxAttrs attrs)
{
    WS53SfcCommandState *s = opaque;
    *value = UINT64_MAX;
    const char *reason = !s->assume_profile ? "profile-disabled" :
                         size != 4 || (off & 3) ? "unsupported-access-width" :
                         "unsupported-register";
    if (s->assume_profile && size == 4 && !(off & 3)) {
        switch (off) {
        case 0x100:
            if (s->configured_handoff) { *value = s->global_config; return MEMTX_OK; }
            break;
        case 0x240:
            if (s->configured_handoff) { *value = s->dma_control; return MEMTX_OK; }
            break;
        case 0x300: *value = s->config; return MEMTX_OK;
        case 0x308: *value = s->instruction; return MEMTX_OK;
        case 0x30c: *value = s->address; return MEMTX_OK;
        }
        if (off >= 0x400 && off < 0x440) {
            *value = ldl_le_p(s->data + off - 0x400);
            return MEMTX_OK;
        }
    }
    return ws53_sfc_reject(s, "read", off, 0, size, reason);
}

static MemTxResult ws53_sfc_write(void *opaque, hwaddr off, uint64_t value,
                                 unsigned size, MemTxAttrs attrs)
{
    WS53SfcCommandState *s = opaque;
    const char *reason;
    if (!s->assume_profile) { reason = "profile-disabled"; goto rejected; }
    if (size != 4 || (off & 3)) { reason = "unsupported-access-width"; goto rejected; }
    if (s->config & 1) { reason = "command-busy"; goto rejected; }
    switch (off) {
    case 0x300:
        if (value & ~WS53_SFC_CONFIG_MASK) {
            reason = "reserved-config-bits"; goto rejected;
        }
        if (value & 1) {
            reason = ws53_sfc_validate(s, value);
            if (reason) { goto rejected; }
            s->command_opcode = s->instruction;
            s->command_address = s->address;
            s->command_length = (value & 0x80) ? ((value >> 9) & 63) + 1 : 0;
            memcpy(s->command_data, s->data, sizeof(s->command_data));
            s->commands_started++;
            if (s->instruction == 0x06 || s->instruction == 0x04 ||
                s->instruction == 0x50 || s->instruction == 0x01 ||
                s->instruction == 0x02 || s->instruction == 0x20) {
                s->mutating_commands_started++;
            }
            s->command_remaining = WS53_SFC_CMD_NS;
        }
        s->config = value;
        ws53_sfc_resume_command(s);
        return MEMTX_OK;
    case 0x308:
        if (value & ~0xffu) { reason = "unsupported-instruction-bits"; goto rejected; }
        s->instruction = value;
        return MEMTX_OK;
    case 0x30c:
        if (value & ~0x3fffffffu) { reason = "reserved-address-bits"; goto rejected; }
        s->address = value;
        return MEMTX_OK;
    }
    if (off >= 0x400 && off < 0x440) {
        stl_le_p(s->data + off - 0x400, value);
        return MEMTX_OK;
    }
    reason = "unsupported-register";
rejected:
    return ws53_sfc_reject(s, "write", off, value, size, reason);
}

/* RDL 92a3d20: same-address halfword/word views of M_CRG+0x570.
 * Upper bits are opaque supplied values, not invented reserved/reset zeros.
 * No writes, even same-value writes: no safe switching protocol is modeled. */
static MemTxResult ws53_sfc_clock_read(void *opaque, hwaddr off,
                                      uint64_t *value, unsigned size,
                                      MemTxAttrs attrs)
{
    WS53SfcCommandState *s = opaque;
    *value = UINT64_MAX;
    if (s->assume_profile && s->configured_handoff && !off &&
        (size == 2 || size == 4)) {
        *value = size == 2 ? s->clock_state & 0xffff : s->clock_state;
        return MEMTX_OK;
    }
    return ws53_sfc_reject(s, "clock-read", off, 0, size,
                          !s->configured_handoff ? "snapshot-disabled" :
                          "unsupported-access-width");
}
static MemTxResult ws53_sfc_clock_write(void *opaque, hwaddr off,
                                       uint64_t value, unsigned size,
                                       MemTxAttrs attrs)
{
    return ws53_sfc_reject(opaque, "clock-write", off, value, size,
                          "clock-writes-not-modeled");
}
static const MemoryRegionOps ws53_sfc_clock_ops = {
    .read_with_attrs = ws53_sfc_clock_read,
    .write_with_attrs = ws53_sfc_clock_write,
    .endianness = DEVICE_LITTLE_ENDIAN,
    .valid = { .min_access_size = 1, .max_access_size = 8, .unaligned = true },
    .impl = { .min_access_size = 1, .max_access_size = 8, .unaligned = true },
};

static MemTxResult ws53_sfc_xip_read(void *opaque, hwaddr off, uint64_t *value,
                                    unsigned size, MemTxAttrs attrs)
{
    *value = UINT64_MAX;
    return ws53_sfc_reject(opaque, "XIP-read", off, 0, size, "XIP-not-modeled");
}
static MemTxResult ws53_sfc_xip_write(void *opaque, hwaddr off, uint64_t value,
                                     unsigned size, MemTxAttrs attrs)
{
    return ws53_sfc_reject(opaque, "XIP-write", off, value, size, "XIP-not-modeled");
}
static const MemoryRegionOps ws53_sfc_ops = {
    .read_with_attrs = ws53_sfc_read, .write_with_attrs = ws53_sfc_write,
    .endianness = DEVICE_LITTLE_ENDIAN,
    .valid = { .min_access_size = 1, .max_access_size = 8, .unaligned = true },
    .impl = { .min_access_size = 1, .max_access_size = 8, .unaligned = true },
};
static const MemoryRegionOps ws53_sfc_xip_ops = {
    .read_with_attrs = ws53_sfc_xip_read, .write_with_attrs = ws53_sfc_xip_write,
    .endianness = DEVICE_LITTLE_ENDIAN,
    .valid = { .min_access_size = 1, .max_access_size = 8, .unaligned = true },
    .impl = { .min_access_size = 1, .max_access_size = 8, .unaligned = true },
};

static void ws53_sfc_abort_command(WS53SfcCommandState *s)
{
    timer_del(s->command_timer);
    s->config &= ~1u;
    s->command_remaining = 0;
}
static void ws53_sfc_controller_reset(void *opaque)
{
    WS53SfcCommandState *s = opaque;
    ws53_sfc_abort_command(s);
    s->config = s->instruction = s->address = 0;
    memset(s->data, 0, sizeof(s->data));
    /* Selected synthetic startup snapshot is replayed after system_reset.
     * This is not measured MCU POR/retention. A busy DMA snapshot stays busy;
     * neither reset nor observation claims to cancel/drain an unknown master. */
    s->global_config = s->initial_global;
    s->dma_control = s->initial_dma;
    s->clock_state = s->initial_clock;
    /* Controller reset does NOT reset the external peer or cancel its work. */
}
static void ws53_sfc_controller_stall(void *opaque, int n, int level)
{
    WS53SfcCommandState *s = opaque;
    ws53_sfc_pause(s->command_timer, &s->command_remaining);
    s->controller_stall = !!level;
    ws53_sfc_resume_command(s);
}
static void ws53_sfc_peer_stall(void *opaque, int n, int level)
{
    WS53SfcCommandState *s = opaque;
    ws53_sfc_pause(s->peer_timer, &s->peer_remaining);
    s->peer_stall = !!level;
    ws53_sfc_resume_peer(s);
}
static void ws53_sfc_power_cycle(void *opaque, int n, int level)
{
    WS53SfcCommandState *s = opaque;
    if (level && !s->power_cycle_level) {
        /* Explicit test input, no claim about a physical partial NOR write.
         * Disconnecting peer also aborts its unfinished controller exchange. */
        ws53_sfc_abort_command(s);
        timer_del(s->peer_timer);
        s->peer_busy = s->peer_wel = s->volatile_enable = false;
        s->sr1_config = s->nv_sr1; s->sr2 = s->nv_sr2;
        s->peer_remaining = 0;
        s->power_cycles++;
        ws53_sfc_update_status(s);
    }
    s->power_cycle_level = !!level;
}
static bool ws53_sfc_get_busy(Object *obj, Error **errp)
{
    return WS53_SFC_COMMAND(obj)->peer_busy;
}
static bool ws53_sfc_get_wel(Object *obj, Error **errp)
{
    return WS53_SFC_COMMAND(obj)->peer_wel;
}
static bool ws53_sfc_get_volatile(Object *obj, Error **errp)
{
    return WS53_SFC_COMMAND(obj)->volatile_enable;
}
static char *ws53_sfc_get_digest(Object *obj, Error **errp)
{
    WS53SfcCommandState *s = WS53_SFC_COMMAND(obj);
    return g_compute_checksum_for_data(G_CHECKSUM_SHA256, s->array, WS53_SFC_NOR_SIZE);
}
static bool ws53_sfc_parse_snapshot(const char *name, const char *text,
                                    uint32_t *result, Error **errp)
{
    uint64_t value;
    if (!text || !g_ascii_isdigit(text[0]) ||
        qemu_strtou64(text, NULL, 0, &value) || value > UINT32_MAX) {
        error_setg(errp, "ws53-sfc-command: %s requires an explicit unsigned 32-bit raw snapshot", name);
        return false;
    }
    *result = value;
    return true;
}
static void ws53_sfc_realize(DeviceState *dev, Error **errp)
{
    WS53SfcCommandState *s = WS53_SFC_COMMAND(dev);
    if (s->snapshot_status3) {
        uint32_t status3;
        if (!s->assume_profile) {
            error_setg(errp, "ws53-sfc-command: snapshot-status3 requires assume-profile");
            return;
        }
        if (!ws53_sfc_parse_snapshot("snapshot-status3", s->snapshot_status3,
                                     &status3, errp)) {
            return;
        }
        if (status3 > UINT8_MAX) {
            error_setg(errp, "ws53-sfc-command: snapshot-status3 exceeds unsigned 8-bit raw status");
            return;
        }
        s->status3 = status3;
        s->has_status3 = true;
    }
    if (s->configured_handoff) {
        if (!s->assume_profile) {
            error_setg(errp, "ws53-sfc-command: configured-handoff requires assume-profile");
            return;
        }
        if (!ws53_sfc_parse_snapshot("snapshot-global", s->snapshot_global,
                                     &s->initial_global, errp) ||
            !ws53_sfc_parse_snapshot("snapshot-dma", s->snapshot_dma,
                                     &s->initial_dma, errp) ||
            !ws53_sfc_parse_snapshot("snapshot-clock", s->snapshot_clock,
                                     &s->initial_clock, errp)) {
            return;
        }
    } else if (s->snapshot_global || s->snapshot_dma || s->snapshot_clock) {
        error_setg(errp, "ws53-sfc-command: raw snapshots require configured-handoff");
        return;
    }
    memset(s->array, 0xff, WS53_SFC_NOR_SIZE);
    if (s->assume_profile) {
        qemu_log_mask(LOG_GUEST_ERROR,
            "ws53-sfc-command: assumed synthetic NOR4MiB ID7f5301 page256 sector4096 "
            "command10us program200us erase1ms status100us; no silicon/NV/drain qualification\n");
    }
    if (s->configured_handoff) {
        qemu_log_mask(LOG_GUEST_ERROR,
            "ws53-sfc-command: assumed configured snapshot global=0x%08x dma=0x%08x clock=0x%08x; "
            "read-only observation, reset replays snapshot; no POR/clock-frequency/ownership/drain qualification\n",
            s->initial_global, s->initial_dma, s->initial_clock);
    }
    if (s->has_status3) {
        qemu_log_mask(LOG_GUEST_ERROR,
            "ws53-sfc-command: assumed immutable synthetic STATUS3=0x%02x; "
            "read15 only, retained across controller reset and peer power cycle; no silicon/NV bit semantics\n",
            s->status3);
    }
    qemu_register_reset(ws53_sfc_controller_reset, s);
}
static const Property ws53_sfc_properties[] = {
    DEFINE_PROP_BOOL("assume-profile", WS53SfcCommandState, assume_profile, false),
    DEFINE_PROP_BOOL("configured-handoff", WS53SfcCommandState, configured_handoff, false),
    DEFINE_PROP_STRING("snapshot-global", WS53SfcCommandState, snapshot_global),
    DEFINE_PROP_STRING("snapshot-dma", WS53SfcCommandState, snapshot_dma),
    DEFINE_PROP_STRING("snapshot-clock", WS53SfcCommandState, snapshot_clock),
    DEFINE_PROP_STRING("snapshot-status3", WS53SfcCommandState, snapshot_status3),
};
static void ws53_sfc_unrealize(DeviceState *dev)
{
    WS53SfcCommandState *s = WS53_SFC_COMMAND(dev);
    qemu_unregister_reset(ws53_sfc_controller_reset, s);
    timer_del(s->command_timer);
    timer_del(s->peer_timer);
}
static void ws53_sfc_finalize(Object *obj)
{
    WS53SfcCommandState *s = WS53_SFC_COMMAND(obj);
    timer_free(s->command_timer);
    timer_free(s->peer_timer);
    g_free(s->array);
}
static void ws53_sfc_init(Object *obj)
{
    WS53SfcCommandState *s = WS53_SFC_COMMAND(obj);
    s->array = g_malloc(WS53_SFC_NOR_SIZE);
    s->command_timer = timer_new_ns(QEMU_CLOCK_VIRTUAL, ws53_sfc_command_complete, s);
    s->peer_timer = timer_new_ns(QEMU_CLOCK_VIRTUAL, ws53_sfc_peer_complete, s);
    memory_region_init_io(&s->mmio, obj, &ws53_sfc_ops, s, TYPE_WS53_SFC_COMMAND, 0x2000);
    memory_region_init_io(&s->xip_rejected, obj, &ws53_sfc_xip_ops, s,
                         "ws53-sfc-XIP-unsupported", WS53_SFC_NOR_SIZE);
    sysbus_init_mmio(SYS_BUS_DEVICE(obj), &s->mmio);
    sysbus_init_mmio(SYS_BUS_DEVICE(obj), &s->xip_rejected);
    memory_region_init_io(&s->clock_observation, obj, &ws53_sfc_clock_ops, s,
                         "ws53-sfc-clock-observation", 4);
    sysbus_init_mmio(SYS_BUS_DEVICE(obj), &s->clock_observation);
    qdev_init_gpio_in_named(DEVICE(obj), ws53_sfc_controller_stall, "controller-stall", 1);
    qdev_init_gpio_in_named(DEVICE(obj), ws53_sfc_peer_stall, "peer-stall", 1);
    qdev_init_gpio_in_named(DEVICE(obj), ws53_sfc_power_cycle, "peer-power-cycle", 1);
    object_property_add_uint64_ptr(obj, "commands-started", &s->commands_started, OBJ_PROP_FLAG_READ);
    object_property_add_uint64_ptr(obj, "commands-completed", &s->commands_completed, OBJ_PROP_FLAG_READ);
    object_property_add_uint64_ptr(obj, "mutating-commands-started", &s->mutating_commands_started, OBJ_PROP_FLAG_READ);
    object_property_add_uint64_ptr(obj, "rejections", &s->rejections, OBJ_PROP_FLAG_READ);
    object_property_add_uint64_ptr(obj, "peer-operations", &s->peer_operations, OBJ_PROP_FLAG_READ);
    object_property_add_uint64_ptr(obj, "peer-commits", &s->peer_commits, OBJ_PROP_FLAG_READ);
    object_property_add_uint64_ptr(obj, "peer-power-cycles", &s->power_cycles, OBJ_PROP_FLAG_READ);
    object_property_add_bool(obj, "peer-busy", ws53_sfc_get_busy, NULL);
    object_property_add_bool(obj, "peer-wel", ws53_sfc_get_wel, NULL);
    object_property_add_bool(obj, "peer-volatile-enable", ws53_sfc_get_volatile, NULL);
    object_property_add_uint8_ptr(obj, "peer-sr1", &s->sr1_live, OBJ_PROP_FLAG_READ);
    object_property_add_uint8_ptr(obj, "peer-sr2", &s->sr2, OBJ_PROP_FLAG_READ);
    object_property_add_uint8_ptr(obj, "peer-nv-sr1", &s->nv_sr1, OBJ_PROP_FLAG_READ);
    object_property_add_uint8_ptr(obj, "peer-nv-sr2", &s->nv_sr2, OBJ_PROP_FLAG_READ);
    object_property_add_str(obj, "peer-sha256", ws53_sfc_get_digest, NULL);
}
static void ws53_sfc_class_init(ObjectClass *oc, WS63_QOM_CLASS_DATA *data)
{
    DeviceClass *dc = DEVICE_CLASS(oc);
    dc->realize = ws53_sfc_realize;
    dc->unrealize = ws53_sfc_unrealize;
    device_class_set_props(dc, ws53_sfc_properties);
}
static const TypeInfo ws53_sfc_type = {
    .name = TYPE_WS53_SFC_COMMAND, .parent = TYPE_SYS_BUS_DEVICE,
    .instance_size = sizeof(WS53SfcCommandState),
    .instance_init = ws53_sfc_init, .instance_finalize = ws53_sfc_finalize,
    .class_init = ws53_sfc_class_init,
};
