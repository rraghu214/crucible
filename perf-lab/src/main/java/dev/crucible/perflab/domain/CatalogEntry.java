package dev.crucible.perflab.domain;

import java.io.Serializable;

/**
 * A catalog row as returned to the client, and as cached in Redis.
 *
 * <p>Serializable because the Redis cache serialises values; a non-serialisable
 * type would fail only under the lab profile and look like a cache bug.
 */
public class CatalogEntry implements Serializable {

    private static final long serialVersionUID = 1L;

    private Long id;
    private String name;
    private Double value;

    public CatalogEntry() {
    }

    public CatalogEntry(Long id, String name, Double value) {
        this.id = id;
        this.name = name;
        this.value = value;
    }

    public Long getId() {
        return id;
    }

    public void setId(Long id) {
        this.id = id;
    }

    public String getName() {
        return name;
    }

    public void setName(String name) {
        this.name = name;
    }

    public Double getValue() {
        return value;
    }

    public void setValue(Double value) {
        this.value = value;
    }
}
