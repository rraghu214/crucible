package dev.crucible.perflab.fixture;

import dev.crucible.perflab.domain.OrderLine;
import dev.crucible.perflab.domain.PurchaseOrder;
import dev.crucible.perflab.domain.PurchaseOrderRepository;

import org.springframework.data.domain.PageRequest;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * Cause family: inefficient_query (the N+1 shape).
 *
 * <p>Reads a page of orders, then touches every order's lazy line collection. One
 * query becomes one-plus-N. The fix is hibernate.default_batch_fetch_size, which
 * is on the allowed list; a larger connection pool does nothing here, and that is
 * exactly what the fixture is testing for.
 */
@Service
public class OrderService {

    private final PurchaseOrderRepository orders;

    public OrderService(PurchaseOrderRepository orders) {
        this.orders = orders;
    }

    @Transactional(readOnly = true)
    public Map<String, Object> summarise(int size) {
        List<PurchaseOrder> page = orders.findPage(PageRequest.of(0, Math.max(1, Math.min(size, 200))));
        double total = 0.0;
        int lineCount = 0;
        for (PurchaseOrder order : page) {
            // This is the N+1: one SELECT per order, issued lazily right here.
            for (OrderLine line : order.getLines()) {
                total += line.getValue() * line.getQuantity();
                lineCount++;
            }
        }
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("orders", page.size());
        body.put("lines", lineCount);
        body.put("total", total);
        return body;
    }
}
