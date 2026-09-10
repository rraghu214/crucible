package dev.crucible.perflab.domain;

import org.springframework.data.domain.Pageable;
import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.data.jpa.repository.Query;

import java.util.List;

public interface PurchaseOrderRepository extends JpaRepository<PurchaseOrder, Long> {

    /**
     * Deliberately no join fetch. The lines collection stays lazy, so the caller
     * that touches it pays one extra query per order. This is the fixture, not an
     * oversight -- see {@link PurchaseOrder}.
     */
    @Query("select o from PurchaseOrder o")
    List<PurchaseOrder> findPage(Pageable pageable);
}
