"""Layer-two check: with smolvm's own allowlist OFF, the uid firewall alone must keep a VM off the LAN.

Run as `smolrunner` by verify.sh. Boots one VM with unrestricted networking (something sandboxd never
does), tries each target from inside it, and exits 1 if anything other than the control opens.
The LAN addresses are this homelab's; adjust them if the network changes.
"""

import sys

import smol

ONE = "import socket,sys; socket.create_connection((sys.argv[1], int(sys.argv[2])), timeout=5).close(); print('OPEN')"


def main() -> int:
    image, host_ip = sys.argv[1], sys.argv[2]
    control = ("public https (control)", "pypi.org", 443)
    forbidden = [
        ("DeepSeek box on the LAN", "192.168.1.46", 8888),
        ("router", "192.168.1.1", 80),
        ("Boodleton on the host LAN IP", host_ip, 8484),
        ("SGLang via the VM gateway (host loopback)", "100.96.0.1", 8889),
        ("cloud metadata", "169.254.169.254", 80),
        ("public, not 443", "1.1.1.1", 80),
    ]
    config = smol.MachineConfig(image=image, resources=smol.ResourceSpec(network=True, cpus=1, memory_mb=1024))
    machine = smol.Machine.create(config, smol.ConnectOptions(target="local"))
    opened: list[str] = []
    try:
        for name, host, port in [control, *forbidden]:
            # Rejected TSI connects hang rather than fail, so each target gets its own bounded exec.
            result = machine.exec(["python3", "-c", ONE, host, str(port)], smol.ExecOptions(timeout=20))
            is_open = "OPEN" in (result.stdout or "")
            print(f"BACKSTOP {name}: {'OPEN' if is_open else 'blocked'}", flush=True)
            if is_open and name != control[0]:
                opened.append(name)
            if not is_open and name == control[0]:
                print("BACKSTOP control did not open: the check proves nothing", flush=True)
                return 2
    finally:
        machine.delete()
    print(f"BACKSTOP {'FAIL: ' + ', '.join(opened) if opened else 'PASS'}", flush=True)
    return 1 if opened else 0


if __name__ == "__main__":
    sys.exit(main())
