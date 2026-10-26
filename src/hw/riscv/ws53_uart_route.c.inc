/* SDK163d9150 uart_port_config_pinmux: complete32-bit command1 at57000048
 * routes L0 only to acore. Readback, other values, reset default and ccore
 * delivery are not evidenced. This profile models command-to-IRQ42 routing,
 * not GLB_CTL wholesale. SPDX-License-Identifier: GPL-2.0-or-later */
#include "qapi/visitor.h"

#define TYPE_WS53_UART_L0_ROUTE "ws53-uart-l0-route"
OBJECT_DECLARE_SIMPLE_TYPE(WS53UartL0RouteState, WS53_UART_L0_ROUTE)

struct WS53UartL0RouteState {
    SysBusDevice parent_obj;
    MemoryRegion mmio;
    qemu_irq irq;
    bool assume_profile, routed, input, blocked;
    uint64_t commands;
};

static void ws53_uart_l0_route_update(WS53UartL0RouteState *s)
{
    /* Default compatibility bypass keeps old UART model profiles unchanged.
     * Explicit routing profile starts unqualified until guest command1. */
    bool deliver = !s->assume_profile || (s->routed && !s->blocked);
    qemu_set_irq(s->irq, s->input && deliver);
}

static void ws53_uart_l0_route_input(void *opaque, int n, int level)
{
    WS53UartL0RouteState *s = opaque;
    s->input = level != 0;
    ws53_uart_l0_route_update(s);
}

static void ws53_uart_l0_route_block(void *opaque, int n, int level)
{
    WS53UartL0RouteState *s = opaque;
    /* Fault input belongs to the model, never a guest register or evidence of
     * silicon behavior. It persists across reset until the test clears it. */
    s->blocked = level != 0;
    ws53_uart_l0_route_update(s);
}

static MemTxResult ws53_uart_l0_route_read(void *opaque, hwaddr off,
                                          uint64_t *value, unsigned size,
                                          MemTxAttrs attrs)
{
    *value = 0;
    qemu_log_mask(LOG_GUEST_ERROR,
        "ws53-uart-l0-route: unsupported read off=0x%" HWADDR_PRIx " size=%u\n", off, size);
    return MEMTX_ERROR;
}

static MemTxResult ws53_uart_l0_route_write(void *opaque, hwaddr off,
                                           uint64_t value, unsigned size,
                                           MemTxAttrs attrs)
{
    WS53UartL0RouteState *s = opaque;
    if (!s->assume_profile || off || size != 4 || value != 1) {
        qemu_log_mask(LOG_GUEST_ERROR,
            "ws53-uart-l0-route: unsupported write off=0x%" HWADDR_PRIx
            " size=%u value=0x%" PRIx64 "\n", off, size, value);
        return MEMTX_ERROR;
    }
    s->commands++;
    s->routed = true;
    ws53_uart_l0_route_update(s);
    return MEMTX_OK;
}

static const MemoryRegionOps ws53_uart_l0_route_ops = {
    .read_with_attrs = ws53_uart_l0_route_read,
    .write_with_attrs = ws53_uart_l0_route_write,
    .endianness = DEVICE_LITTLE_ENDIAN,
    /* Reject unsupported transactions whole; do not split a wide access into
     * an accepted command plus a neighboring partial operation. */
    .valid = { .min_access_size = 1, .max_access_size = 8, .unaligned = true },
    .impl = { .min_access_size = 1, .max_access_size = 8, .unaligned = true },
};

static void ws53_uart_l0_route_reset(void *opaque)
{
    WS53UartL0RouteState *s = opaque;
    /* Explicit profile policy, NOT an observed hardware reset value. Keep the
     * source IRQ level and external model fault input independent of routing. */
    s->routed = false;
    s->commands = 0;
    ws53_uart_l0_route_update(s);
}

static void ws53_uart_l0_route_realize(DeviceState *dev, Error **errp)
{
    WS53UartL0RouteState *s = WS53_UART_L0_ROUTE(dev);
    if (s->assume_profile) {
        error_report("ws53-uart-l0-route: explicit command1-to-acore profile; "
                     "initial route, ccore destination and physical readback are not modeled");
    }
    qemu_register_reset(ws53_uart_l0_route_reset, s);
}

static const Property ws53_uart_l0_route_properties[] = {
    DEFINE_PROP_BOOL("assume-profile", WS53UartL0RouteState, assume_profile, false),
};

static bool ws53_uart_l0_route_get_routed(Object *obj, Error **errp)
{
    return WS53_UART_L0_ROUTE(obj)->routed;
}
static bool ws53_uart_l0_route_get_input(Object *obj, Error **errp)
{
    return WS53_UART_L0_ROUTE(obj)->input;
}
static bool ws53_uart_l0_route_get_blocked(Object *obj, Error **errp)
{
    return WS53_UART_L0_ROUTE(obj)->blocked;
}
static void ws53_uart_l0_route_get_commands(Object *obj, Visitor *v,
                                           const char *name, void *opaque,
                                           Error **errp)
{
    uint64_t value = WS53_UART_L0_ROUTE(obj)->commands;
    visit_type_uint64(v, name, &value, errp);
}

static void ws53_uart_l0_route_init(Object *obj)
{
    WS53UartL0RouteState *s = WS53_UART_L0_ROUTE(obj);
    memory_region_init_io(&s->mmio, obj, &ws53_uart_l0_route_ops, s,
                          TYPE_WS53_UART_L0_ROUTE, 8);
    sysbus_init_mmio(SYS_BUS_DEVICE(obj), &s->mmio);
    sysbus_init_irq(SYS_BUS_DEVICE(obj), &s->irq);
    qdev_init_gpio_in_named(DEVICE(obj), ws53_uart_l0_route_input, "source", 1);
    qdev_init_gpio_in_named(DEVICE(obj), ws53_uart_l0_route_block, "block-route", 1);
    object_property_add_bool(obj, "routed", ws53_uart_l0_route_get_routed, NULL);
    object_property_add_bool(obj, "source-pending", ws53_uart_l0_route_get_input, NULL);
    object_property_add_bool(obj, "blocked", ws53_uart_l0_route_get_blocked, NULL);
    object_property_add(obj, "commands", "uint64", ws53_uart_l0_route_get_commands,
                        NULL, NULL, NULL);
}

static void ws53_uart_l0_route_class_init(ObjectClass *oc, WS63_QOM_CLASS_DATA *data)
{
    DeviceClass *dc = DEVICE_CLASS(oc);
    dc->realize = ws53_uart_l0_route_realize;
    device_class_set_props(dc, ws53_uart_l0_route_properties);
}

static const TypeInfo ws53_uart_l0_route_type = {
    .name = TYPE_WS53_UART_L0_ROUTE, .parent = TYPE_SYS_BUS_DEVICE,
    .instance_size = sizeof(WS53UartL0RouteState),
    .instance_init = ws53_uart_l0_route_init,
    .class_init = ws53_uart_l0_route_class_init,
};
