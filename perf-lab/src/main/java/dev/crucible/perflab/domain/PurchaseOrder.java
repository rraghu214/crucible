package dev.crucible.perflab.domain;

import jakarta.persistence.CascadeType;
import jakarta.persistence.Entity;
import jakarta.persistence.FetchType;
import jakarta.persistence.GeneratedValue;
import jakarta.persistence.GenerationType;
import jakarta.persistence.Id;
import jakarta.persistence.OneToMany;

import java.util.ArrayList;
import java.util.List;

/**
 * The N+1 fixture, half of it.
 *
 * <p>Lines are deliberately {@code LAZY} with no join fetch anywhere. Reading a
 * page of orders and touching their lines issues one query for the orders and
 * then one per order -- the classic N+1 shape. It is fixed by
 * {@code hibernate.default_batch_fetch_size}, which is on the profile's allowed
 * list, and is <em>not</em> fixed by a bigger connection pool. That asymmetry is
 * the point: it separates an agent that read the evidence from one that reaches
 * for the pool every time.
 *
 * <p>Named PurchaseOrder rather than Order because {@code order} is a reserved
 * word in both Postgres and H2.
 */
@Entity
public class PurchaseOrder {

    @Id
    @GeneratedValue(strategy = GenerationType.IDENTITY)
    private Long id;

    private String reference;

    @OneToMany(mappedBy = "order", cascade = CascadeType.ALL, fetch = FetchType.LAZY)
    private List<OrderLine> lines = new ArrayList<>();

    public Long getId() {
        return id;
    }

    public void setId(Long id) {
        this.id = id;
    }

    public String getReference() {
        return reference;
    }

    public void setReference(String reference) {
        this.reference = reference;
    }

    public List<OrderLine> getLines() {
        return lines;
    }

    public void setLines(List<OrderLine> lines) {
        this.lines = lines;
    }
}
