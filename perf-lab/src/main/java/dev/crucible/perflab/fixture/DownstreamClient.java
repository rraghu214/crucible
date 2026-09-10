package dev.crucible.perflab.fixture;

import dev.crucible.perflab.PerfLabProperties;

import org.springframework.http.client.SimpleClientHttpRequestFactory;
import org.springframework.stereotype.Component;
import org.springframework.web.client.RestClient;

import java.time.Duration;
import java.util.Map;

/**
 * Cause family: downstream_latency.
 *
 * <p>Calls httpbin's /delay/{n}. The application is not slow here -- it is
 * waiting. This is the family the agent is most likely to misattribute, because
 * every symptom it can see locally (rising latency, busy threads, a pool under
 * pressure) is real; the cause simply is not in this process.
 *
 * <p>Raising the connection pool here makes things measurably worse by admitting
 * more concurrent waiters, which is what makes this a good trap: the wrong fix is
 * not merely useless, it is detectable as a regression.
 *
 * <p>The client is built from Boot's injected {@link RestClient.Builder} rather
 * than from a fresh one, because that builder carries the observation registry.
 * Build it standalone and http.client.requests never appears -- leaving the agent
 * with no metric that names the downstream at all, which would make "the app is
 * slow" unfalsifiable rather than merely wrong.
 */
@Component
public class DownstreamClient {

    private final RestClient client;
    private final PerfLabProperties properties;

    public DownstreamClient(RestClient.Builder builder, PerfLabProperties properties) {
        this.properties = properties;
        SimpleClientHttpRequestFactory factory = new SimpleClientHttpRequestFactory();
        factory.setConnectTimeout(Duration.ofMillis(properties.getDownstream().getTimeoutMs()));
        factory.setReadTimeout(Duration.ofMillis(properties.getDownstream().getTimeoutMs()));
        this.client = builder
                .baseUrl(properties.getDownstream().getBaseUrl())
                .requestFactory(factory)
                .build();
    }

    public Map<String, Object> call() {
        double delay = properties.getDownstream().getDelaySeconds();
        @SuppressWarnings("unchecked")
        Map<String, Object> body = client.get()
                .uri("/delay/{seconds}", delay)
                .retrieve()
                .body(Map.class);
        return body == null ? Map.of() : body;
    }
}
