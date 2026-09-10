package dev.crucible.perflab.fixture;

import dev.crucible.perflab.Item;
import dev.crucible.perflab.ItemRepository;
import dev.crucible.perflab.PerfLabProperties;
import dev.crucible.perflab.domain.CatalogEntry;

import org.springframework.cache.annotation.Cacheable;
import org.springframework.stereotype.Service;

import java.util.List;

/**
 * Cause family: cache_miss.
 *
 * <p>The read is expensive on purpose. With the cache on, only the first call per
 * key pays that cost; with perflab.cache.enabled=false every call does, and the
 * fix is a configuration flag rather than more capacity.
 *
 * <p>Note for the agent's benefit: a disabled cache and a cache with a poor hit
 * ratio look similar in latency and different in cache.gets. That distinction is
 * the whole test.
 */
@Service
public class CatalogService {

    private final ItemRepository itemRepository;
    private final PerfLabProperties properties;

    public CatalogService(ItemRepository itemRepository, PerfLabProperties properties) {
        this.itemRepository = itemRepository;
        this.properties = properties;
    }

    @Cacheable(cacheNames = "catalog", key = "#page", condition = "@perfLabProperties.cache.enabled")
    public List<CatalogEntry> page(int page) {
        return load(page);
    }

    private List<CatalogEntry> load(int page) {
        // A deliberately unindexed, deliberately slow read. Real catalogs do this
        // with a join across three tables; the cost is what matters, not the shape.
        List<Item> items = itemRepository.findAll();
        int from = Math.min(page * 20, items.size());
        int to = Math.min(from + 20, items.size());
        try {
            Thread.sleep(35);
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
        }
        return items.subList(from, to).stream()
                .map(i -> new CatalogEntry(i.getId(), i.getName(), i.getValue()))
                .toList();
    }
}
