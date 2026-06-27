## WS63 / BS2X QEMU emulator (`-M ws63`, `-M bs21`, `-M bs21e`, `-M bs22`, `-M bs20`)

`qemu-system-riscv32` (fork of QEMU __QEMU_TAG__) with the HiSilicon
**WS63 / BS2X** machine models and the **xlinx custom RISC-V ISA**, so
unmodified vendor-compiled firmware runs without hardware.

### Per-host downloads (runnable, libs bundled)

| Host | Asset |
|------|-------|
| Linux x86_64   | `hisi-riscv-qemu-x86_64-linux.tar.gz` |
| Linux aarch64  | `hisi-riscv-qemu-aarch64-linux.tar.gz` |
| macOS (Apple Silicon) | `hisi-riscv-qemu-aarch64-darwin.tar.gz` |
| Windows x86_64 | `hisi-riscv-qemu-x86_64-windows.zip` (experimental) |

Legacy `qemu-system-riscv32-ws63-*` (raw Ubuntu x86_64 binary) and the
OS-independent `ws63-qemu-src-*.tar.gz` source bundle (rebuild any host via
`scripts/build.sh`; this is how Intel-mac users build) are also attached.

**macOS:** binaries are unsigned — clear quarantine after extracting:
`xattr -dr com.apple.quarantine hisi-riscv-qemu-aarch64-darwin/`

### Run

```
./qemu-system-riscv32 -M ws63 -nographic -serial mon:stdio -kernel <firmware.elf>
```

📖 Documentation: <https://hispark-rs.github.io/hisi-riscv-qemu/>
