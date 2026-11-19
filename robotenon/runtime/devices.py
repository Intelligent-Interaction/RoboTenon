"""Logical GPU slots resolve to physical devices only at process boundaries."""

def validate_gpu_map(value):
    if not isinstance(value, dict):
        raise ValueError("virtual_gpu_map must be an object")
    for alias, physical in value.items():
        if (not isinstance(alias, str) or not alias.strip() or alias != alias.strip()
                or "," in alias or alias.isdigit() or alias.startswith(("GPU-", "MIG-"))):
            raise ValueError("virtual GPU names must be nonempty aliases, not physical GPU IDs")
        if (not isinstance(physical, str) or not physical
                or not (physical.isdigit() or physical.startswith(("GPU-", "MIG-")))
                or "," in physical or physical != physical.strip()):
            raise ValueError("virtual GPU targets must be physical indices or GPU/MIG UUIDs")
    return dict(value)


def physical_device(device, mapping):
    if device is None:
        return None
    device = str(device)
    if mapping and device not in mapping and not (
            device.isdigit() or device.startswith(("GPU-", "MIG-"))):
        raise ValueError(f"unknown virtual GPU: {device}")
    return mapping.get(device, device)
