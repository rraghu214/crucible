package dev.crucible.perflab;

import dev.crucible.perflab.domain.OrderLine;
import dev.crucible.perflab.domain.PurchaseOrder;
import dev.crucible.perflab.domain.PurchaseOrderRepository;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.boot.CommandLineRunner;
import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;
import org.springframework.cache.annotation.EnableCaching;
import org.springframework.context.annotation.Bean;
import org.springframework.scheduling.annotation.EnableAsync;

import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.ThreadLocalRandom;

@SpringBootApplication
@EnableAsync
@EnableCaching
public class PerfLabApplication {

    private static final Logger log = LoggerFactory.getLogger(PerfLabApplication.class);

    /** Orders seeded for the N+1 fixture, and lines per order. */
    private static final int ORDER_COUNT = 400;
    private static final int LINES_PER_ORDER = 6;

    public static void main(String[] args) {
        SpringApplication.run(PerfLabApplication.class, args);
    }

    /**
     * Seeds 100 Item rows on startup so /api/db and /api/catalog have stable data.
     */
    @Bean
    CommandLineRunner seedItems(ItemRepository itemRepository) {
        return args -> {
            if (itemRepository.count() > 0) {
                return;
            }
            for (int i = 1; i <= 100; i++) {
                Item item = new Item();
                item.setName("item-" + i);
                item.setValue(ThreadLocalRandom.current().nextDouble(0.0, 1000.0));
                itemRepository.save(item);
            }
            log.info("Seeded {} Item rows", itemRepository.count());
        };
    }

    /**
     * Seeds the N+1 fixture. The row count is fixed rather than random because two
     * campaigns run against different amounts of data are not comparable, and a
     * drifting fixture would show up as an unexplained latency trend that has
     * nothing to do with any change the agent made.
     */
    @Bean
    CommandLineRunner seedOrders(PurchaseOrderRepository orders) {
        return args -> {
            if (orders.count() > 0) {
                return;
            }
            List<PurchaseOrder> batch = new ArrayList<>(ORDER_COUNT);
            for (int i = 1; i <= ORDER_COUNT; i++) {
                PurchaseOrder order = new PurchaseOrder();
                order.setReference("PO-" + i);
                for (int line = 1; line <= LINES_PER_ORDER; line++) {
                    OrderLine orderLine = new OrderLine();
                    orderLine.setSku("SKU-" + i + "-" + line);
                    orderLine.setQuantity(line);
                    orderLine.setValue(line * 12.5);
                    orderLine.setOrder(order);
                    order.getLines().add(orderLine);
                }
                batch.add(order);
            }
            orders.saveAll(batch);
            log.info("Seeded {} PurchaseOrder rows with {} lines each", orders.count(), LINES_PER_ORDER);
        };
    }
}
