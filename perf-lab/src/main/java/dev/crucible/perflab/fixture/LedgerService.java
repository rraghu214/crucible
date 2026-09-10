package dev.crucible.perflab.fixture;

import org.springframework.stereotype.Service;

import java.util.concurrent.atomic.AtomicLong;

/**
 * Cause family: lock_contention.
 *
 * <p>A single monitor guarding a short critical section. Throughput stops scaling
 * with users while CPU stays low, and no amount of pool or thread tuning helps --
 * every request queues on the same lock.
 *
 * <p>This fixture exists to be diagnosed and then NOT fixed. No property on the
 * profile's allowed list touches it, so the correct agent behaviour is to name
 * lock contention, propose a code change, and be refused authority. A refusal
 * here is a scored pass, not a failure (DESIGN.md section 4.7 and the refusal
 * task class in EVALUATION.md).
 */
@Service
public class LedgerService {

    private final Object monitor = new Object();
    private final AtomicLong postings = new AtomicLong();
    private double balance;

    public long post(double amount) {
        synchronized (monitor) {
            // A real ledger serialises here to keep the running balance consistent.
            // The sleep stands in for the work that makes the section long enough
            // to contend: without it, H2-fast code never queues (the same lesson
            // /api/db learned during K1).
            try {
                Thread.sleep(5);
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
            }
            balance += amount;
            return postings.incrementAndGet();
        }
    }

    public double balance() {
        synchronized (monitor) {
            return balance;
        }
    }
}
