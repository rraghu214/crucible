package dev.crucible.perflab.fixture;

import dev.crucible.perflab.domain.CatalogEntry;

import org.springframework.stereotype.Service;

import java.util.ArrayList;
import java.util.List;

/**
 * Cause family: payload_serialization.
 *
 * <p>Latency scales with response size rather than with concurrency: CPU is high
 * while the pool and the thread pool are both healthy. That combination is the
 * whole signature, and it is the one an agent anchored on "latency means
 * contention" will misread.
 */
@Service
public class PayloadService {

    /** Bounded so a stray load profile cannot ask for a gigabyte of JSON. */
    public static final int MAX_ROWS = 20_000;

    public List<CatalogEntry> generate(int rows) {
        int bounded = Math.max(1, Math.min(rows, MAX_ROWS));
        List<CatalogEntry> out = new ArrayList<>(bounded);
        for (int i = 0; i < bounded; i++) {
            out.add(new CatalogEntry((long) i, "row-" + i + "-" + "x".repeat(64), i * 1.5));
        }
        return out;
    }
}
