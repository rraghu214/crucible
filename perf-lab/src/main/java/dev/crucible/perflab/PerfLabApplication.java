package dev.crucible.perflab;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.boot.CommandLineRunner;
import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;
import org.springframework.context.annotation.Bean;

import java.util.concurrent.ThreadLocalRandom;

@SpringBootApplication
public class PerfLabApplication {

    private static final Logger log = LoggerFactory.getLogger(PerfLabApplication.class);

    public static void main(String[] args) {
        SpringApplication.run(PerfLabApplication.class, args);
    }

    /**
     * Seeds 100 Item rows on startup so /api/db has stable data to read.
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
}
