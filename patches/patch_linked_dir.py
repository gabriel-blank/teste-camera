from pathlib import Path
import anomalib.utils.path as utils_path


def create_versioned_dir_no_link(root_dir):
    root = Path(root_dir)
    root.mkdir(parents=True, exist_ok=True)
    return root


utils_path.create_versioned_dir = create_versioned_dir_no_link