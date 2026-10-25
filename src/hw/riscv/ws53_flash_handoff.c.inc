/* Qualified application-entry profile, NOT silicon reset defaults.
 * SDK 163d9150: dyn_mem.c, acore_memory_config.h, memory_config_common.h;
 * physical A-board SRST entry observation flash-entry-observe-04 (2026-10-24).
 * Only the observed 0x20 -> MCU_MODE_1 0x3c transition is implemented. No
 * alternate ITCM aliases, control CPU, ROM, loader, SFC command or authentication.
 * SPDX-License-Identifier: GPL-2.0-or-later
 */

static void ws53_memory_routes(WS53MachineState *s)
{
    memory_region_transaction_begin();
    for (unsigned i = 0; i < 7; i++) {
        /* SDK banks2..5: 32KiB each; interleaved6/7,8/9,10/11: 64KiB each.
         * Banks12/13 and the control-core address space remain unmapped. */
        bool enabled = i < 4 ? (s->dtcm_share & (1u << (i + 2))) :
            ((s->bram_share & (1u << (i - 4 + 4))) &&
             !(s->bram_share & (1u << (i - 4))));
        memory_region_set_enabled(&s->dtcm_bank[i], enabled);
    }
    memory_region_transaction_commit();
}

static uint64_t ws53_memory_read(void *opaque, hwaddr off, unsigned size)
{
    WS53MachineState *s = opaque;
    if (size == 4) {
        switch (off) {
        case 0x234: return s->bram_share;
        case 0x238: return s->dtcm_share;
        case 0x604: return 0; /* qualified MCU_MODE_1 field */
        case 0x96c: return s->ccore_reset;
        }
    }
    qemu_log_mask(LOG_GUEST_ERROR,
        "ws53-memory: unimplemented read +0x%" HWADDR_PRIx " size=%u\n", off, size);
    return 0;
}

static void ws53_memory_write(void *opaque, hwaddr off, uint64_t v, unsigned size)
{
    WS53MachineState *s = opaque;
    if (size == 4) {
        switch (off) {
        case 0x234:
            /* Preserve observed control offset3; ownership changes require
             * the affected pair's gate OFF before and during the write. */
            if ((v & ~0xffu) != 0x300 ||
                (((v ^ s->bram_share) & 15) & ((v | s->bram_share) >> 4))) {
                break;
            }
            s->bram_share = v;
            ws53_memory_routes(s);
            return;
        case 0x238:
            if (v != 0x20 && v != 0x3c) { break; }
            s->dtcm_share = v;
            ws53_memory_routes(s);
            return;
        case 0x604:
            if (v == 0) { return; }
            break;
        case 0x96c:
            /* Qualified application-owned profile only: 0 holds all domains,
             * 3 releases logic/CRG with the control CPU still held reset.
             * SDK main.c copy_to_ccore uses these values and a 5us settle;
             * precise hardware settle timing is not modeled. No CPU release
             * or vendor firmware execution is implemented here. */
            if (v == 0 || v == 3) {
                s->ccore_reset = v;
                ws53_tsensor_set_domain(s->tsensor, v == 3);
                return;
            }
            break;
        }
    }
    qemu_log_mask(LOG_GUEST_ERROR,
        "ws53-memory: unimplemented write +0x%" HWADDR_PRIx
        " value=0x%" PRIx64 " size=%u\n", off, v, size);
}

static const MemoryRegionOps ws53_memory_ops = {
    .read = ws53_memory_read, .write = ws53_memory_write,
    .endianness = DEVICE_LITTLE_ENDIAN,
    .valid = { .min_access_size = 1, .max_access_size = 8, .unaligned = true },
    .impl = { .min_access_size = 1, .max_access_size = 8, .unaligned = true },
};

static MemTxResult ws53_blocked_read(void *opaque, hwaddr off, uint64_t *value,
                                    unsigned size, MemTxAttrs attrs)
{
    qemu_log_mask(LOG_GUEST_ERROR,
        "ws53-memory: inaccessible DTCM read +0x%" HWADDR_PRIx "\n", off);
    *value = 0;
    return MEMTX_ERROR;
}

static MemTxResult ws53_blocked_write(void *opaque, hwaddr off, uint64_t value,
                                     unsigned size, MemTxAttrs attrs)
{
    qemu_log_mask(LOG_GUEST_ERROR,
        "ws53-memory: inaccessible DTCM write +0x%" HWADDR_PRIx "\n", off);
    return MEMTX_ERROR;
}

static const MemoryRegionOps ws53_blocked_ops = {
    .read_with_attrs = ws53_blocked_read, .write_with_attrs = ws53_blocked_write,
    .endianness = DEVICE_LITTLE_ENDIAN,
    .valid = { .min_access_size = 1, .max_access_size = 8, .unaligned = true },
    .impl = { .min_access_size = 1, .max_access_size = 8, .unaligned = true },
};

static void ws53_flash_handoff_init(WS53MachineState *s, MemoryRegion *sys)
{
    memory_region_init_ram(&s->dtcm, NULL, "ws53.handoff.dtcm", 0x50000, &error_fatal);
    memory_region_init_io(&s->dtcm_blocked, OBJECT(s), &ws53_blocked_ops, s,
                         "ws53.handoff.inaccessible", 0x50000);
    memory_region_add_subregion_overlap(sys, 0x20010000, &s->dtcm_blocked, -1);
    for (unsigned i = 0; i < 7; i++) {
        uint32_t offset = i < 4 ? i * 0x8000 : 0x20000 + (i - 4) * 0x10000;
        g_autofree char *name = g_strdup_printf("ws53.handoff.bank%u", i);
        memory_region_init_alias(&s->dtcm_bank[i], OBJECT(s), name,
            &s->dtcm, offset, i < 4 ? 0x8000 : 0x10000);
        memory_region_add_subregion_overlap(sys, 0x20010000 + offset, &s->dtcm_bank[i], 0);
    }
    /* Narrow aliases prevent masking neighboring eFuse programming-status
     * MMIO at 0x57030258. All holes remain genuinely unmapped. */
    const unsigned offsets[] = { 0x234, 0x604, 0x96c };
    for (unsigned i = 0; i < ARRAY_SIZE(offsets); i++) {
        g_autofree char *name = g_strdup_printf("ws53.handoff.config%u", i);
        memory_region_init_io(&s->memory_config[i], OBJECT(s),
            &ws53_memory_ops, s, name, 0x1000);
        /* Use a region alias so callbacks receive the documented offsets. */
    }
    /* The actual mappings are aliases of these private register views. */
    for (unsigned i = 0; i < ARRAY_SIZE(offsets); i++) {
        g_autofree char *name = g_strdup_printf("ws53.handoff.view%u", i);
        memory_region_init_alias(&s->memory_view[i], OBJECT(s), name,
            &s->memory_config[i], offsets[i], i == 0 ? 8 : 4);
        memory_region_add_subregion(sys, 0x57030000 + offsets[i], &s->memory_view[i]);
    }
    memory_region_init_rom(&s->flash, NULL, "ws53.handoff.flash", 0x400000, &error_fatal);
    memory_region_add_subregion(sys, 0x400000, &s->flash);
}

static void ws53_flash_handoff_reset(WS53MachineState *s)
{
    s->ccore_reset = 0;
    ws53_tsensor_set_domain(s->tsensor, false);
    s->bram_share = 0x3f0;
    s->dtcm_share = 0x20;
    /* Deliberate test poison, NOT a prediction of SRAM contents at reset.
     * Prevent an ELF loader's zero-fill from proving guest initialization. */
    memset(memory_region_get_ram_ptr(&s->itcm), 0xa5, 0x10000);
    memset(memory_region_get_ram_ptr(&s->dtcm), 0xa5, 0x50000);
    ws53_memory_routes(s);
    s->mclken1->value = 0xffff; /* observed FlashBoot gate snapshot */
    ws53_mclken1_update(s->mclken1);
    /* SDK start_fastboot feeds seven seconds before entering the application.
     * Remaining time/low byte use the explicit WDT timing assumption; the
     * observed control register is 0x3d. This is not exact hardware timing. */
    s->wdt->load = (7 * 32000 - 1) & ~0xffu;
    s->wdt->control = 0x3d;
    s->wdt->counting = true;
    ws53_wdt_timing(s->wdt);
    ws53_wdt_reload(s->wdt);
}
