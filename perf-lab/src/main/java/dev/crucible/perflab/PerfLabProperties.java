package dev.crucible.perflab;

import org.springframework.boot.context.properties.ConfigurationProperties;
import org.springframework.stereotype.Component;

/**
 * The perflab.* knobs the Crucible agent is allowed to turn.
 *
 * <p>Every property here also appears in config/profiles/spring-boot.yaml. That
 * file is the authority; this class is only how Spring binds them. Adding a knob
 * here does NOT grant the agent permission to change it.
 */
// Registered as a @Component, not via @EnableConfigurationProperties, so the bean
// name is the predictable "perfLabProperties". CatalogService's @Cacheable
// condition references it by that name in SpEL; the generated name that
// @EnableConfigurationProperties produces would silently fail to resolve.
@Component
@ConfigurationProperties(prefix = "perflab")
public class PerfLabProperties {

    /** Version reported by /api/version. */
    private String version = "0.0.0";

    private final Cache cache = new Cache();
    private final Downstream downstream = new Downstream();
    private final Churn churn = new Churn();

    public String getVersion() {
        return version;
    }

    public void setVersion(String version) {
        this.version = version;
    }

    public Cache getCache() {
        return cache;
    }

    public Downstream getDownstream() {
        return downstream;
    }

    public Churn getChurn() {
        return churn;
    }

    public static class Cache {
        /**
         * When false, /api/catalog reads through to the database every time. This
         * is a real deployment mistake, not a synthetic one -- a cache that was
         * switched off during an incident and never switched back on.
         */
        private boolean enabled = true;

        public boolean isEnabled() {
            return enabled;
        }

        public void setEnabled(boolean enabled) {
            this.enabled = enabled;
        }
    }

    public static class Downstream {
        /** Base URL of the simulated third party. httpbin under the lab profile. */
        private String baseUrl = "http://localhost:8081";

        /** Per-call timeout. Raising the pool size while this is high makes latency worse. */
        private int timeoutMs = 5000;

        /** Delay httpbin is asked to introduce, in seconds. */
        private double delaySeconds = 0.4;

        public String getBaseUrl() {
            return baseUrl;
        }

        public void setBaseUrl(String baseUrl) {
            this.baseUrl = baseUrl;
        }

        public int getTimeoutMs() {
            return timeoutMs;
        }

        public void setTimeoutMs(int timeoutMs) {
            this.timeoutMs = timeoutMs;
        }

        public double getDelaySeconds() {
            return delaySeconds;
        }

        public void setDelaySeconds(double delaySeconds) {
            this.delaySeconds = delaySeconds;
        }
    }

    public static class Churn {
        /** Bytes allocated per request on /api/churn. */
        private int allocationBytes = 1_048_576;

        /** How many allocations are retained, to push survivors into the old generation. */
        private int retainedObjects = 64;

        public int getAllocationBytes() {
            return allocationBytes;
        }

        public void setAllocationBytes(int allocationBytes) {
            this.allocationBytes = allocationBytes;
        }

        public int getRetainedObjects() {
            return retainedObjects;
        }

        public void setRetainedObjects(int retainedObjects) {
            this.retainedObjects = retainedObjects;
        }
    }
}
