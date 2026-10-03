# Backup-X

Backup-X is a student project to build a local file-backup application. It watches selected folders, stores file contents as chunks, avoids storing duplicate chunks, and helps users restore a chosen snapshot.

> **Project status:** Planning and initial setup. Features and setup commands below describe the project goals; they are not yet implemented.

## Why Backup-X?

When files change, making a complete copy of a folder for every backup can use unnecessary disk space. Backup-X is designed to split file contents into chunks and identify them with SHA-256 hashes. When a chunk already exists, a later backup can refer to that stored chunk instead of saving another copy. Snapshots record the state of a folder at a point in time so that files can be restored later.

## Project goals

- Back up selected folders to local storage.
- Detect file changes and process updated files.
- Divide file contents into chunks.
- Identify duplicate chunks with SHA-256 hashes and reuse stored data.
- Track chunk and snapshot metadata in MySQL.
- Restore files from a selected snapshot and verify reconstructed data.
- Provide a simple web interface for backup and recovery actions.

## Planned technology

| Area | Technology |
| --- | --- |
| Backend | Java, Spring Boot |
| Database | MySQL |
| Frontend | HTML, CSS, JavaScript |
| File monitoring | Java `WatchService` |
| Chunk identification | SHA-256 |
| Backup data | Local filesystem storage |

The application is planned for Windows and Linux. Exact versions and configuration will be documented when the starter project is created.

## High-level workflow

```text
Select a folder
      ↓
Monitor the folder for changes
      ↓
Read changed files and divide their contents into chunks
      ↓
Calculate a SHA-256 hash for each chunk
      ↓
Check whether each chunk is already stored
      ├── Existing chunk: reuse its stored reference
      └── New chunk: save it to local chunk storage
      ↓
Record file, chunk, and snapshot metadata in MySQL
      ↓
Restore: retrieve snapshot chunks, verify, and rebuild files
```

## Team responsibilities

Each member develops their module on a separate PC. The team will use one GitHub repository and integrate work through feature branches and pull requests. The following division follows the project proposal; agree on module interfaces before implementation.

| Member | Main module | OS responsibility | DBMS responsibility |
| --- | --- | --- | --- |
| **P1 — Team Lead** | Backup engine and architecture | File handling, file I/O, chunking | Chunk metadata and basic database interaction |
| **P2** | File monitoring and concurrency | `WatchService`, threads, `ExecutorService`, locks | Transactions and concurrency |
| **P3** | Database and snapshot manager | Recovery-related OS/file handling | Schema, normalization, keys, indexes, queries |
| **P4** | Recovery and web interface | File reconstruction, I/O, integrity | Snapshot and recovery queries |

### Module boundaries

- P1 processes selected files and hands chunks to the agreed chunk-processing interface. P1 also uses the agreed metadata interface for chunk lookups or recording, as coordinated with P3.
- P2 owns folder monitoring and concurrent event handling.
- P3 owns the MySQL schema, metadata persistence, and snapshot management.
- P4 owns restore/reconstruction and the user-facing web interface.
- Shared interfaces and data types must be agreed and documented before modules are connected. Shared configuration and build files should have a designated maintainer.

## Development milestones

1. **Core setup and monitoring** — Create the shared Spring Boot project, agree on interfaces and storage paths, and build basic folder monitoring and file-copy flow.
2. **Deduplication and snapshots** — Add chunk reuse using SHA-256, track snapshots, and process background work.
3. **Recovery and testing** — Restore files from chunks, handle failures safely, and verify the integrated application.

## Repository layout

The repository will use a single backend project and keep modules in separate packages. The exact package names may be adjusted when the Spring Boot starter project is added.

```text
Backup-X/
├── README.md
├── backend/
│   └── src/main/java/com/backupx/
│       ├── backup/       # P1: backup workflow
│       ├── file/         # P1: file handling and chunk input
│       ├── monitoring/   # P2: folder monitoring and workers
│       ├── chunk/        # Chunk processing and storage; coordinate ownership
│       ├── metadata/     # P3: database and snapshot metadata
│       ├── restore/      # P4: recovery and reconstruction
│       └── web/          # P4: web interface/API
└── frontend/             # If maintained separately from Spring Boot resources
```

## Getting started

The GitHub repository is currently being initialized. There is no runnable application yet. Once the team commits the Spring Boot starter project, this section should include:

1. Required Java, Maven/Gradle, and MySQL versions.
2. Database creation and local configuration steps.
3. How to start the backend and frontend.
4. How to run the agreed verification steps.

Do not commit real database passwords, API keys, or machine-specific absolute paths. Keep local secrets in an ignored configuration file or environment variables and provide a safe example configuration for teammates.

## Git collaboration workflow

1. Clone this repository on your PC.
2. Update the shared integration branch before starting work.
3. Create a feature branch for your module, for example `feature/p1-backup-engine`.
4. Commit focused changes and push the feature branch.
5. Open a pull request into the team integration branch and request a review.
6. Pull the merged integration branch before starting the next piece of work.
7. Merge the integration branch into `main` when the team considers a stable milestone ready.

Avoid editing another member's owned files without coordinating first. Discuss changes to shared files such as the Maven build file and application configuration with their maintainer.

## Project assumptions

- The user has permission to read the selected source files and enough disk space for backups.
- MySQL stores metadata; local filesystem storage holds chunk contents.
- The first version is intended for students and small teams.

## References

- [Java WatchService API](https://docs.oracle.com/en/java/javase/21/docs/api/java.base/java/nio/file/WatchService.html)
- [Java concurrency utilities](https://docs.oracle.com/en/java/javase/21/docs/api/java.base/java/util/concurrent/package-summary.html)
- [Spring Boot documentation](https://docs.spring.io/spring-boot/documentation.html)
- [MySQL Reference Manual](https://dev.mysql.com/doc/refman/8.4/en/)
- [NIST Secure Hash Standard (SHA-256)](https://doi.org/10.6028/NIST.FIPS.180-4)
