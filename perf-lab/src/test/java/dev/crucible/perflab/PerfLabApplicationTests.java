package dev.crucible.perflab;

import dev.crucible.perflab.fixture.CatalogService;
import dev.crucible.perflab.fixture.DownstreamClient;
import dev.crucible.perflab.fixture.OrderService;

import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * The context loads with no containers running.
 *
 * <p>That is the assertion, not a formality. The default profile has to start on
 * H2 with no cache and no downstream, because CI has no Docker and a laptop
 * should not need it. Every fixture bean is injected here so a wiring mistake
 * fails in a test rather than at the start of a load run -- DownstreamClient in
 * particular takes Boot's RestClient.Builder, which only resolves if the web
 * starter's autoconfiguration is present.
 */
@SpringBootTest
class PerfLabApplicationTests {

    @Autowired
    private CatalogService catalogService;

    @Autowired
    private DownstreamClient downstreamClient;

    @Autowired
    private OrderService orderService;

    @Autowired
    private PerfLabProperties properties;

    @Test
    void contextLoadsWithoutAnyContainers() {
        assertThat(catalogService).isNotNull();
        assertThat(downstreamClient).isNotNull();
        assertThat(orderService).isNotNull();
    }

    @Test
    void theNPlusOneFixtureIsSeededAndReadable() {
        // 400 orders x 6 lines. Fixed, because two campaigns run against
        // different amounts of data are not comparable.
        var summary = orderService.summarise(50);

        assertThat(summary.get("orders")).isEqualTo(50);
        assertThat(summary.get("lines")).isEqualTo(300);
    }

    @Test
    void batchFetchIsOffByDefaultSoTheNPlusOneActuallyExists() {
        // If this ever reads > 1, /api/orders has silently stopped being a
        // fixture and the agent would be scored on a bottleneck that is not there.
        assertThat(System.getProperty("spring.jpa.properties.hibernate.default_batch_fetch_size"))
                .isNull();
        assertThat(properties.getVersion()).isEqualTo("0.2.0");
    }
}
