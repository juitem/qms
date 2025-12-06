```mermaid
flowchart TD
    A[Start] --> B[Parse arguments<br>image, rootfs_dir, output_dir]
    B --> C[Check image file exists]
    C --> D[Check rootfs_dir exists]
    D --> E[Check debugfs in PATH]
    E --> F{--skip-extract?}
    F -- No --> G[Run debugfs<br>"rdump / OUTPUT_DIR"]
    F -- Yes --> H[Skip extraction]
    G --> I{debugfs success?}
    I -- No --> Z[Print error and exit]
    I -- Yes --> J[Proceed to symlink rewriting]
    H --> J

    J --> K{--skip-symlinks?}
    K -- Yes --> Y[Print 'skip symlinks' and exit OK]
    K -- No --> L[Walk OUTPUT_DIR tree]

    L --> M[For each entry: lstat]
    M --> N{is symlink?}
    N -- No --> L
    N -- Yes --> O[readlink target]

    O --> P{target starts with "/"?}
    P -- No --> L
    P -- Yes --> Q[Compute host_abs_target = rootfs_dir + target.lstrip("/")]
    Q --> R[Compute rel_target = relpath(host_abs_target, dirpath)]
    R --> S[Remove old symlink]
    S --> T[Create new symlink with rel_target]
    T --> L

    L --> U[Finished walk]
    U --> V[Print 'Done']
    V --> W[End]
```