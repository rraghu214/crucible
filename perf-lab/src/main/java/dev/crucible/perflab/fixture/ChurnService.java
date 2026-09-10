package dev.crucible.perflab.fixture;

import dev.crucible.perflab.PerfLabProperties;

import org.springframework.stereotype.Service;

import java.util.ArrayDeque;
import java.util.Deque;
import java.util.concurrent.ThreadLocalRandom;

/**
 * Cause family: gc_pressure.
 *
 * <p>Allocates a large buffer per request and retains a bounded window of them, so
 * a fraction survive young collection and are promoted. The signature is a p99 far
 * worse than p95 while the mean stays acceptable, and pauses that are periodic
 * rather than proportional to load.
 *
 * <p>The retention window is bounded deliberately. An unbounded leak would end the
 * run in an OutOfMemoryError, which is a different fixture and a different
 * diagnosis -- and would make the measurement useless rather than bad.
 */
@Service
public class ChurnService {

    private final PerfLabProperties properties;
    private final Deque<byte[]> retained = new ArrayDeque<>();

    public ChurnService(PerfLabProperties properties) {
        this.properties = properties;
    }

    public long churn() {
        int size = properties.getChurn().getAllocationBytes();
        byte[] block = new byte[size];
        // Touch the buffer so the JVM cannot optimise the allocation away.
        block[ThreadLocalRandom.current().nextInt(size)] = 1;
        synchronized (retained) {
            retained.addLast(block);
            while (retained.size() > properties.getChurn().getRetainedObjects()) {
                retained.removeFirst();
            }
            return (long) retained.size() * size;
        }
    }
}
