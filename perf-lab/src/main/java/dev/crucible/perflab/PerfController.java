package dev.crucible.perflab;

import org.springframework.beans.factory.annotation.Value;
import org.springframework.transaction.annotation.Transactional;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

import java.time.Instant;
import java.util.LinkedHashMap;
import java.util.Map;

@RestController
@RequestMapping("/api")
public class PerfController {

    private final ItemRepository itemRepository;

    @Value("${spring.application.name}")
    private String appName;

    @Value("${perflab.version}")
    private String version;

    @Value("${spring.datasource.hikari.maximum-pool-size}")
    private int poolSize;

    public PerfController(ItemRepository itemRepository) {
        this.itemRepository = itemRepository;
    }

    /**
     * No database access. Baseline for latency comparison.
     */
    @GetMapping("/fast")
    public Map<String, Object> fast() {
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("status", "ok");
        body.put("timestamp", Instant.now().toString());
        return body;
    }

    /**
     * Single DB round-trip via the connection pool. The method is transactional
     * so the pooled connection stays checked out for the whole call, including
     * the simulated latency below — H2 in-memory returns in microseconds, which
     * is unrepresentative of a real Spring Boot service hitting Postgres
     * (50-200ms is typical). Without the hold the pool never queues.
     */
    @GetMapping("/db")
    @Transactional
    public Map<String, Object> db() throws InterruptedException {
        long count = itemRepository.count();
        Thread.sleep(50); // simulate realistic DB latency
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("count", count);
        body.put("timestamp", Instant.now().toString());
        return body;
    }

    /**
     * App identity and the current pool size (read from resolved config / env).
     */
    @GetMapping("/version")
    public VersionInfo version() {
        return new VersionInfo(appName, version, poolSize);
    }
}
