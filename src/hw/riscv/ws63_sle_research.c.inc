/* SPDX-License-Identifier: GPL-2.0-or-later
 * Opt-in software-contract research model, NOT a qualified SLE controller.
 * Firmware evidence and assumptions: docs/reference/ws63-sle-research.md.
 * No radio, fabricated packets, successful connections, or PHY emulation.
 */
#define TYPE_WS63_SLE_RESEARCH "ws63-sle-research"
OBJECT_DECLARE_SIMPLE_TYPE(WS63SleResearch, WS63_SLE_RESEARCH)
struct WS63SleResearch {
    SysBusDevice parent_obj;
    MemoryRegion mmio, em;
    uint32_t words[0x10000 / 4];
    uint32_t tick_ns;
    QEMUTimer *target_timer;
    qemu_irq irq;
    char *trace_path;
    FILE *trace_file;
};

static void sle_research_trace(WS63SleResearch *s, char op, hwaddr off,
                               unsigned size, uint64_t value)
{
    if (s->trace_file) {
        fprintf(s->trace_file,
                "{\"ns\":%" PRId64 ",\"pc\":%" PRIu64
                ",\"op\":\"%c\",\"address\":%" PRIu64
                ",\"size\":%u,\"value\":%" PRIu64 "}\n",
                qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL),
                current_cpu ? (uint64_t)RISCV_CPU(current_cpu)->env.pc : 0,
                op, (uint64_t)(0x49000000 + off), size, value);
        fflush(s->trace_file);
    }
}

static void sle_research_irq(WS63SleResearch *s)
{
    /* Only the ES target bit has a modeled source. */
    qemu_set_irq(s->irq, !!(s->words[0x1410 / 4] &
                           s->words[0x1414 / 4] & (1u << 5)));
}

static void sle_research_target(void *opaque)
{
    WS63SleResearch *s = opaque;
    s->words[0x1414 / 4] |= 1u << 5;
    sle_research_trace(s, 'E', 0x1414, 4, s->words[0x1414 / 4]);
    sle_research_irq(s);
}

static uint64_t sle_research_read(void *opaque, hwaddr off, unsigned size)
{
    WS63SleResearch *s = opaque;
    uint32_t value = s->words[off / 4] >> ((off & 3) * 8);
    if (size < 4) {
        value &= (1u << (size * 8)) - 1;
    }
    sle_research_trace(s, 'R', off, size, value);
    return value;
}

static void sle_research_write(void *opaque, hwaddr off, uint64_t value,
                                unsigned size)
{
    WS63SleResearch *s = opaque;
    uint32_t shift = (off & 3) * 8;
    uint32_t mask = size == 4 ? UINT32_MAX : ((1u << (size * 8)) - 1);
    uint32_t *word = &s->words[off / 4];
    sle_research_trace(s, 'W', off, size, value);
    /* Unmodeled locations are explicitly scratch storage, NOT proven RW. */
    *word = (*word & ~(mask << shift)) | (((uint32_t)value & mask) << shift);
    if (size != 4 || (off & 3)) {
        return;
    }
    switch (off) {
    case 0x1500:
        if (value & (1u << 31)) {
            uint64_t now = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
            /* Immediate capture completion and down-count fine phase are
             * model assumptions; the ROM only proves set/poll/read. */
            *word = (*word & (1u << 30)) |
                    ((now / s->tick_ns) & 0x3fffffff);
            s->words[0x1504 / 4] =
                MIN((s->tick_ns - 1 - now % s->tick_ns) / 1000, 127);
        }
        break;
    case 0x14fc: {
        uint64_t now = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
        uint32_t ticks = ((uint32_t)value - now / s->tick_ns) & 0x3fffffff;
        /* Target commits on write; equal/past/wrap semantics not verified. */
        timer_mod(s->target_timer, now + (uint64_t)MAX(ticks, 1) * s->tick_ns);
        break;
    }
    case 0x141c:
        /* ACK behavior follows firmware's observed write-status pattern. */
        s->words[0x1414 / 4] &= ~(value & (1u << 5));
        sle_research_irq(s);
        break;
    case 0x1410:
        sle_research_irq(s);
        break;
    }
}

static const MemoryRegionOps sle_research_ops = {
    .read = sle_research_read,
    .write = sle_research_write,
    .endianness = DEVICE_LITTLE_ENDIAN,
    .valid = { .min_access_size = 1, .max_access_size = 4,
               .unaligned = false },
    .impl = { .min_access_size = 1, .max_access_size = 4 },
};

static void sle_research_reset(DeviceState *dev)
{
    WS63SleResearch *s = WS63_SLE_RESEARCH(dev);
    /* Deterministic model initial state, not measured silicon reset values. */
    memset(s->words, 0, sizeof(s->words));
    memset(memory_region_get_ram_ptr(&s->em), 0, 0x10000);
    timer_del(s->target_timer);
    qemu_set_irq(s->irq, 0);
}

static void sle_research_realize(DeviceState *dev, Error **errp)
{
    WS63SleResearch *s = WS63_SLE_RESEARCH(dev);
    Object *intc = object_resolve_path("/machine/intc", NULL);
    if (!intc || !object_dynamic_cast(OBJECT(qdev_get_machine()),
                                     MACHINE_TYPE_NAME("ws63"))) {
        error_setg(errp, "ws63-sle-research requires the WS63 machine");
        return;
    }
    if (s->tick_ns < 1000 || s->tick_ns > 125000) {
        error_setg(errp, "coarse-tick-ns must be 1000..125000 (model assumption)");
        return;
    }
    if (s->trace_path) {
        s->trace_file = fopen(s->trace_path, "w");
        if (!s->trace_file) {
            error_setg_errno(errp, errno, "cannot open SLE research trace");
            return;
        }
    }
    memory_region_init_io(&s->mmio, OBJECT(s), &sle_research_ops, s,
                          "ws63.sle-research.mmio", 0x10000);
    memory_region_add_subregion_overlap(get_system_memory(), 0x49000000,
                                        &s->mmio, 1);
    memory_region_init_ram(&s->em, OBJECT(s), "ws63.sle-research.em",
                           0x10000, &error_fatal);
    memory_region_add_subregion_overlap(get_system_memory(), 0x49010000,
                                        &s->em, 1);
    s->irq = qdev_get_gpio_in(DEVICE(intc), 47);
    s->target_timer = timer_new_ns(QEMU_CLOCK_VIRTUAL, sle_research_target, s);
}

static const Property sle_research_properties[] = {
    DEFINE_PROP_UINT32("coarse-tick-ns", WS63SleResearch, tick_ns, 125000),
    DEFINE_PROP_STRING("trace-file", WS63SleResearch, trace_path),
};

static void sle_research_class_init(ObjectClass *oc, void *data)
{
    DeviceClass *dc = DEVICE_CLASS(oc);
    dc->user_creatable = true;
    dc->hotpluggable = false;
    dc->realize = sle_research_realize;
    device_class_set_legacy_reset(dc, sle_research_reset);
    device_class_set_props(dc, sle_research_properties);
    dc->desc = "Experimental WS63 SLE software-contract model (no radio)";
}

static const TypeInfo sle_research_type = {
    .name = TYPE_WS63_SLE_RESEARCH,
    .parent = TYPE_SYS_BUS_DEVICE,
    .instance_size = sizeof(WS63SleResearch),
    .class_init = sle_research_class_init,
};

static void sle_research_register_types(void)
{
    type_register_static(&sle_research_type);
}
type_init(sle_research_register_types)
