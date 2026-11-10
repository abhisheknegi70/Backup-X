package com.backupx.backup;

import com.backupx.metadata.*;
import com.backupx.file.FileChunker;
import com.backupx.hash.HashUtil;
import com.backupx.metadata.*;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.io.File;
import java.io.FileOutputStream;
import java.io.IOException;
import java.nio.file.Path;
import java.time.LocalDateTime;

@Service
public class BackupService {

    private final ChunkRepository chunkRepository;
    private final SnapshotRepository snapshotRepository;
    private final FileEntryRepository fileEntryRepository;
    private final FileChunkRepository fileChunkRepository;

    @Value("${backupx.storage.path}")
    private String storagePath;

    public BackupService(ChunkRepository chunkRepository,
                         SnapshotRepository snapshotRepository,
                         FileEntryRepository fileEntryRepository,
                         FileChunkRepository fileChunkRepository) {
        this.chunkRepository = chunkRepository;
        this.snapshotRepository = snapshotRepository;
        this.fileEntryRepository = fileEntryRepository;
        this.fileChunkRepository = fileChunkRepository;
    }

    @Transactional
    public Snapshot createSnapshot(String name) {
        Snapshot snapshot = new Snapshot();
        snapshot.setSnapshotName(name);
        snapshot.setStatus("IN_PROGRESS");
        return snapshotRepository.save(snapshot);
    }


    @Transactional
    public void markSourceDeleted(Path filePath) {
        String originalPath = filePath.toAbsolutePath()
                .normalize()
                .toString();

        FileEntry latestEntry = fileEntryRepository
                .findByOriginalPathOrderByFileIdDesc(originalPath)
                .stream()
                .findFirst()
                .orElse(null);

        if (latestEntry == null) {
            System.out.println(
                    "No backup record found for deleted file: "
                            + originalPath);
            return;
        }

        latestEntry.setSourceDeleted(true);
        fileEntryRepository.save(latestEntry);

        System.out.println(
                "Marked source as deleted; backup retained: "
                        + originalPath);
    }

    @Transactional
    public void completeSnapshot(Long snapshotId) {
        Snapshot snapshot = snapshotRepository.findById(snapshotId)
                .orElseThrow(() ->
                        new IllegalArgumentException(
                                "Snapshot not found: " + snapshotId));

        snapshot.setStatus("COMPLETED");
        snapshotRepository.save(snapshot);
    }

    @Transactional
    public void failSnapshot(Long snapshotId) {
        Snapshot snapshot = snapshotRepository.findById(snapshotId)
                .orElseThrow(() ->
                        new IllegalArgumentException(
                                "Snapshot not found: " + snapshotId));

        snapshot.setStatus("FAILED");
        snapshotRepository.save(snapshot);
    }

    @Transactional
    public void processFile(Path filePath, Snapshot snapshot) throws IOException {
        File file = filePath.toFile();

        // 1. Record the file entry in the database
        FileEntry fileEntry = new FileEntry();
        fileEntry.setSnapshot(snapshot);
        fileEntry.setOriginalPath(filePath.toString());
        fileEntry.setFileSize(file.length());
        // Note: Make sure you generate Getters and Setters in FileEntry.java!
        FileEntry savedFileEntry = fileEntryRepository.save(fileEntry);

        final Long fileId = savedFileEntry.getFileId();


        // 2. Process chunks
        FileChunker.forEachChunk(
                filePath,
                FileChunker.DEFAULT_CHUNK_SIZE,
                (offset, chunkData) -> {

                    String hash = HashUtil.calculateSHA256(chunkData);

                    // 3. Find an existing chunk or save a new one
                    Chunk chunkEntity = chunkRepository.findById(hash)
                            .orElseGet(() -> {
                                saveChunkToDisk(hash, chunkData);

                                Chunk newChunk = new Chunk();
                                newChunk.setHashId(hash);
                                newChunk.setSizeBytes((long) chunkData.length);
                                newChunk.setStoragePath(
                                        new File(storagePath, hash).getAbsolutePath()
                                );

                                return chunkRepository.save(newChunk);
                            });

                    // 4. Map this chunk to the file entry
                    int chunkOrder =
                            (int) (offset / FileChunker.DEFAULT_CHUNK_SIZE);

                    FileChunkId fcId =
                            new FileChunkId(fileId, chunkOrder);

                    FileChunk fileChunk = new FileChunk();
                    fileChunk.setId(fcId);

                    // Required for @MapsId("fileId")
                    fileChunk.setFileEntry(savedFileEntry);

                    // Required to associate the stored chunk
                    fileChunk.setChunk(chunkEntity);

                    fileChunkRepository.save(fileChunk);
                });

    }

    private void saveChunkToDisk(String hash, byte[] data) {
        File targetFile = new File(storagePath, hash);
        targetFile.getParentFile().mkdirs();
        try (FileOutputStream fos = new FileOutputStream(targetFile)) {
            fos.write(data);
        } catch (IOException e) {
            throw new RuntimeException("Failed to write chunk to disk", e);
        }
    }
}