package dev.crucible.perflab.fixture;

import org.springframework.scheduling.annotation.Async;
import org.springframework.stereotype.Service;

import java.util.concurrent.CompletableFuture;

/**
 * Cause family: thread_pool_saturation.
 *
 * <p>Offloads to Spring's task executor, whose core-size, max-size and
 * queue-capacity are all on the profile's allowed list. Under load the queue
 * builds, and requests wait before any application work starts -- distinguishable
 * from pool exhaustion because the wait is visible in executor.queued rather than
 * in hikaricp acquire.
 */
@Service
public class AsyncWorkService {

    @Async
    public CompletableFuture<Long> compute(long input) {
        long started = System.nanoTime();
        try {
            Thread.sleep(40);
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
        }
        return CompletableFuture.completedFuture((System.nanoTime() - started) / 1_000_000);
    }
}
