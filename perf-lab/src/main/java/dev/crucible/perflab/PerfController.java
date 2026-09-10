package dev.crucible.perflab;

import dev.crucible.perflab.domain.CatalogEntry;
import dev.crucible.perflab.fixture.AsyncWorkService;
import dev.crucible.perflab.fixture.CatalogService;
import dev.crucible.perflab.fixture.ChurnService;
import dev.crucible.perflab.fixture.DownstreamClient;
import dev.crucible.perflab.fixture.LedgerService;
import dev.crucible.perflab.fixture.OrderService;
import dev.crucible.perflab.fixture.PayloadService;

import org.springframework.beans.factory.annotation.Value;
import org.springframework.transaction.annotation.Transactional;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

import java.time.Instant;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * PerfLab's endpoint surface: one endpoint per cause family, plus a baseline.
 *
 * <p>The families are declared in config/profiles/spring-boot.yaml, not here. If
 * you add an endpoint, add its family there too -- an endpoint whose cause is not
 * a declared family is a fixture the agent cannot possibly name correctly.
 *
 * <table>
 *   <tr><td>/api/fast</td>      <td>baseline, no dependency</td></tr>
 *   <tr><td>/api/db</td>        <td>connection_pool_exhaustion</td></tr>
 *   <tr><td>/api/async</td>     <td>thread_pool_saturation</td></tr>
 *   <tr><td>/api/churn</td>     <td>gc_pressure</td></tr>
 *   <tr><td>/api/orders</td>    <td>inefficient_query (N+1)</td></tr>
 *   <tr><td>/api/catalog</td>   <td>cache_miss</td></tr>
 *   <tr><td>/api/downstream</td><td>downstream_latency</td></tr>
 *   <tr><td>/api/ledger</td>    <td>lock_contention</td></tr>
 *   <tr><td>/api/payload</td>   <td>payload_serialization</td></tr>
 * </table>
 */
@RestController
@RequestMapping("/api")
public class PerfController {

    private final ItemRepository itemRepository;
    private final OrderService orderService;
    private final CatalogService catalogService;
    private final DownstreamClient downstreamClient;
    private final LedgerService ledgerService;
    private final ChurnService churnService;
    private final AsyncWorkService asyncWorkService;
    private final PayloadService payloadService;

    @Value("${spring.application.name}")
    private String appName;

    @Value("${perflab.version}")
    private String version;

    @Value("${spring.datasource.hikari.maximum-pool-size}")
    private int poolSize;

    public PerfController(ItemRepository itemRepository,
                          OrderService orderService,
                          CatalogService catalogService,
                          DownstreamClient downstreamClient,
                          LedgerService ledgerService,
                          ChurnService churnService,
                          AsyncWorkService asyncWorkService,
                          PayloadService payloadService) {
        this.itemRepository = itemRepository;
        this.orderService = orderService;
        this.catalogService = catalogService;
        this.downstreamClient = downstreamClient;
        this.ledgerService = ledgerService;
        this.churnService = churnService;
        this.asyncWorkService = asyncWorkService;
        this.payloadService = payloadService;
    }

    /**
     * No dependency of any kind. The comparison point that says whether a
     * regression is in the application or in something it talks to.
     */
    @GetMapping("/fast")
    public Map<String, Object> fast() {
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("status", "ok");
        body.put("timestamp", Instant.now().toString());
        return body;
    }

    /**
     * Cause family: connection_pool_exhaustion.
     *
     * <p>Transactional, so the pooled connection stays checked out across the
     * simulated latency below. Postgres and H2 both return in microseconds for a
     * count; without the hold the pool never queues, which is exactly what the K1
     * spike found before the sleep was added.
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

    /** Cause family: thread_pool_saturation. */
    @GetMapping("/async")
    public Map<String, Object> async() throws Exception {
        long elapsedMs = asyncWorkService.compute(1L).get();
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("elapsed_ms", elapsedMs);
        return body;
    }

    /** Cause family: gc_pressure. */
    @GetMapping("/churn")
    public Map<String, Object> churn() {
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("retained_bytes", churnService.churn());
        return body;
    }

    /** Cause family: inefficient_query (N+1). */
    @GetMapping("/orders")
    public Map<String, Object> orders(@RequestParam(defaultValue = "50") int size) {
        return orderService.summarise(size);
    }

    /** Cause family: cache_miss. */
    @GetMapping("/catalog")
    public List<CatalogEntry> catalog(@RequestParam(defaultValue = "0") int page) {
        return catalogService.page(page);
    }

    /** Cause family: downstream_latency. */
    @GetMapping("/downstream")
    public Map<String, Object> downstream() {
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("downstream", downstreamClient.call().keySet());
        return body;
    }

    /** Cause family: lock_contention. Diagnosable, deliberately not fixable by config. */
    @GetMapping("/ledger")
    public Map<String, Object> ledger() {
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("posting", ledgerService.post(1.0));
        body.put("balance", ledgerService.balance());
        return body;
    }

    /** Cause family: payload_serialization. */
    @GetMapping("/payload")
    public List<CatalogEntry> payload(@RequestParam(defaultValue = "5000") int rows) {
        return payloadService.generate(rows);
    }

    /** App identity and the current pool size (read from resolved config / env). */
    @GetMapping("/version")
    public VersionInfo version() {
        return new VersionInfo(appName, version, poolSize);
    }
}
