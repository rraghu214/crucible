package dev.crucible.perflab;

import org.springframework.beans.factory.annotation.Value;
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
     * Single DB round-trip via the connection pool.
     */
    @GetMapping("/db")
    public Map<String, Object> db() {
        long count = itemRepository.count();
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
