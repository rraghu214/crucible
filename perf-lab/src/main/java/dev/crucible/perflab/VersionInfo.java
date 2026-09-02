package dev.crucible.perflab;

/**
 * Response body for GET /api/version.
 */
public class VersionInfo {

    private String app;
    private String version;
    private int poolSize;

    public VersionInfo() {
    }

    public VersionInfo(String app, String version, int poolSize) {
        this.app = app;
        this.version = version;
        this.poolSize = poolSize;
    }

    public String getApp() {
        return app;
    }

    public void setApp(String app) {
        this.app = app;
    }

    public String getVersion() {
        return version;
    }

    public void setVersion(String version) {
        this.version = version;
    }

    public int getPoolSize() {
        return poolSize;
    }

    public void setPoolSize(int poolSize) {
        this.poolSize = poolSize;
    }
}
