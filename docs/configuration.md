# Configuration

The tool reads one TOML file, the first found of:

1. `--config PATH`
2. `$MBKN_BTRFS_RESCUE_CONFIG`
3. `./mbkn-btrfs-rescue.toml` (current directory)
4. `mbkn-btrfs-rescue.toml` in the project checkout
5. `~/.config/mbkn-btrfs-rescue/config.toml` (respects `$XDG_CONFIG_HOME`)

`scripts/setup.sh` copies [`mbkn-btrfs-rescue.example.toml`](../mbkn-btrfs-rescue.example.toml)
to `./mbkn-btrfs-rescue.toml` (gitignored — it is machine specific).
`scripts/setup.sh --device /dev/...` also fills in the device.

| key | default | |
|---|---|---|
| `device` | — | device or image file; only ever opened read-only |
| `tmp_dir` | `~/.tmp/mbkn-btrfs-rescue` | scratch space (test images, Python `tempfile`) |
| `cache_dir` | `~/.cache/mbkn-btrfs-rescue` | the SQLite index lives here |
| `db_name` | `index.sqlite` | index file name inside `cache_dir` |
| `exclude` | `[".venv", ".venv-tools"]` | names hidden from browse / mount / restore |

`~` and `$VARS` are expanded in paths. Command-line options (`-d`, `--db`, `-x`,
`--no-exclude`) override the file.

Nothing secret is stored anywhere; the index contains file names and metadata from the
recovered filesystem, so treat `cache_dir` with the same care as the disk itself and delete it
when done.
