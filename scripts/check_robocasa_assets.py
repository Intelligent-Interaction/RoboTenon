"""Check for missing object categories before starting a RoboCasa batch."""

import json


def main():
    from robocasa.models.objects.kitchen_object_utils import OBJ_CATEGORIES

    missing = [
        name for name, registries in OBJ_CATEGORIES.items()
        if not any(meta.mjcf_paths for meta in registries.values())
    ]
    print(json.dumps({"categories": len(OBJ_CATEGORIES), "missing": missing}, indent=2))
    if missing:
        raise SystemExit("Missing RoboCasa object assets; install the missing asset registry before evaluation.")


if __name__ == "__main__":
    main()
