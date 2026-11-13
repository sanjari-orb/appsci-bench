"""BundleManager for creating tar.gz bundles for data transfer.

This module handles creating compressed archives from task directories
that are then transferred into Docker containers. The bundle contains
all the files needed by the task environment (fixtures, data, scripts).
"""

import tarfile
import tempfile
from pathlib import Path


class BundleManager:
    """Creates tar.gz bundles for transferring data into containers.

    The BundleManager reads a bundle configuration (list of src/dest mappings)
    and creates a tar.gz archive. When extracted in the container, files are
    placed at their specified destination paths.

    Bundle config example:
        [
            {"src": "seed_data/fixture/", "dest": "/fixture/"},
            {"src": "environment/data/", "dest": "/data/"},
            {"src": "environment/db/", "dest": "/scripts/"},
        ]

    The resulting tarball structure mirrors the 'dest' paths so that
    extracting with `tar -xzf bundle.tar.gz -C /` places files correctly.

    Example usage:
        manager = BundleManager()
        bundle_path = manager.create_bundle(
            task_dir=Path("tasks/order_to_cash"),
            bundle_config=[{"src": "seed_data/fixture/", "dest": "/fixture/"}],
            output_dir=Path("/tmp")
        )
        # bundle_path = /tmp/o2c-bundle.tar.gz
    """

    def create_bundle(
        self,
        task_dir: Path,
        bundle_config: list[dict],
        output_dir: Path,
    ) -> Path:
        """Create a tar.gz bundle based on the bundle configuration.

        Reads files from source directories (relative to task_dir) and
        packages them into a tarball with paths matching the destination.

        Args:
            task_dir: Path to the task directory (e.g., tasks/order_to_cash/).
                Source paths in bundle_config are relative to this directory.
            bundle_config: List of source-to-destination mappings.
                Each entry is a dict with:
                - 'src': Source path relative to task_dir (e.g., "seed_data/fixture/")
                - 'dest': Destination path in container (e.g., "/fixture/")
            output_dir: Directory where the bundle tarball will be written.

        Returns:
            Path to the created bundle tarball (e.g., /tmp/o2c-bundle.tar.gz).

        Raises:
            FileNotFoundError: If a source directory in bundle_config doesn't exist.
            PermissionError: If output_dir is not writable.

        Example:
            bundle_config = [
                {"src": "seed_data/fixture/", "dest": "/fixture/"},
                {"src": "environment/data/", "dest": "/data/"},
            ]
            path = manager.create_bundle(
                task_dir=Path("tasks/order_to_cash"),
                bundle_config=bundle_config,
                output_dir=Path("/tmp")
            )

        Implementation notes:
            - The tarball should be extractable with: tar -xzf bundle.tar.gz -C /
            - Preserve file permissions and directory structure
            - Handle both files and directories in src paths
            - Strip leading slash from dest when adding to tarball
        """
        bundle_name = "bundle.tar.gz"
        bundle_path = output_dir / bundle_name

        with tarfile.open(bundle_path, "w:gz") as tar:
            for entry in bundle_config:
                src_path = task_dir / entry["src"]
                dest_path = entry["dest"].lstrip("/")  # Remove leading slash for tarball

                if not src_path.exists():
                    raise FileNotFoundError(f"Source path does not exist: {src_path}")

                if src_path.is_dir():
                    # Add all files in directory
                    for file_path in src_path.rglob("*"):
                        if file_path.is_file():
                            # Calculate arcname: dest_path + relative path from src
                            rel_path = file_path.relative_to(src_path)
                            arcname = str(Path(dest_path) / rel_path)
                            tar.add(file_path, arcname=arcname)
                else:
                    # Add single file
                    arcname = str(Path(dest_path) / src_path.name)
                    tar.add(src_path, arcname=arcname)

        return bundle_path
