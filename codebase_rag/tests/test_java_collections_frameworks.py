"""
Java Collections Framework and collection usage testing.
Tests collection implementations, custom collections, iterator patterns,
map operations, set operations, generic collections, and collection utilities.
"""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers


@pytest.fixture
def java_collections_project(temp_repo: Path) -> Path:
    """Create a Java project with collections framework usage."""
    project_path = temp_repo / "java_collections_test"
    project_path.mkdir()

    # Create standard Java project structure
    (project_path / "src").mkdir()
    (project_path / "src" / "main").mkdir()
    (project_path / "src" / "main" / "java").mkdir()
    (project_path / "src" / "main" / "java" / "com").mkdir()
    (project_path / "src" / "main" / "java" / "com" / "example").mkdir()

    return project_path


def test_basic_collection_implementations(
    java_collections_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test basic Java collection implementations (ArrayList, HashMap, TreeSet, etc.)."""
    test_file = (
        java_collections_project
        / "src"
        / "main"
        / "java"
        / "com"
        / "example"
        / "BasicCollections.java"
    )
    test_file.write_text(
        """
package com.example;

import java.util.*;

public class BasicCollections {

    public void demonstrateArrayList() {
        // ArrayList operations
        List<String> names = new ArrayList<>();
        names.add("Alice");
        names.add("Bob");
        names.add("Charlie");

        // Access operations
        String first = names.get(0);
        int size = names.size();
        boolean contains = names.contains("Bob");

        // Modification operations
        names.set(1, "Robert");
        names.remove("Charlie");
        names.clear();
    }

    public void demonstrateLinkedList() {
        // LinkedList operations
        LinkedList<Integer> numbers = new LinkedList<>();
        numbers.addFirst(1);
        numbers.addLast(3);
        numbers.add(1, 2); // Insert at index

        // Queue operations
        numbers.offer(4);
        Integer head = numbers.poll();
        Integer peek = numbers.peek();

        // Stack operations
        numbers.push(5);
        Integer pop = numbers.pop();
    }

    public void demonstrateHashMap() {
        // HashMap operations
        Map<String, Integer> scores = new HashMap<>();
        scores.put("Alice", 95);
        scores.put("Bob", 87);
        scores.put("Charlie", 92);

        // Access operations
        Integer aliceScore = scores.get("Alice");
        boolean hasKey = scores.containsKey("Bob");
        boolean hasValue = scores.containsValue(95);

        // Key and value sets
        Set<String> keys = scores.keySet();
        Collection<Integer> values = scores.values();
        Set<Map.Entry<String, Integer>> entries = scores.entrySet();

        // Iteration
        for (Map.Entry<String, Integer> entry : entries) {
            String name = entry.getKey();
            Integer score = entry.getValue();
            System.out.println(name + ": " + score);
        }
    }

    public void demonstrateTreeMap() {
        // TreeMap for sorted keys
        TreeMap<String, String> sortedMap = new TreeMap<>();
        sortedMap.put("zebra", "Zebra");
        sortedMap.put("apple", "Apple");
        sortedMap.put("banana", "Banana");

        // Navigation operations
        String firstKey = sortedMap.firstKey();
        String lastKey = sortedMap.lastKey();
        Map.Entry<String, String> firstEntry = sortedMap.firstEntry();
        Map.Entry<String, String> lastEntry = sortedMap.lastEntry();

        // Range operations
        SortedMap<String, String> headMap = sortedMap.headMap("c");
        SortedMap<String, String> tailMap = sortedMap.tailMap("b");
    }

    public void demonstrateHashSet() {
        // HashSet operations
        Set<String> uniqueWords = new HashSet<>();
        uniqueWords.add("hello");
        uniqueWords.add("world");
        uniqueWords.add("hello"); // Duplicate, won't be added

        // Set operations
        boolean added = uniqueWords.add("java");
        boolean removed = uniqueWords.remove("world");
        int size = uniqueWords.size();

        Set<String> otherSet = new HashSet<>(Arrays.asList("java", "python", "go"));

        // Set operations
        Set<String> union = new HashSet<>(uniqueWords);
        union.addAll(otherSet);

        Set<String> intersection = new HashSet<>(uniqueWords);
        intersection.retainAll(otherSet);

        Set<String> difference = new HashSet<>(uniqueWords);
        difference.removeAll(otherSet);
    }

    public void demonstrateTreeSet() {
        // TreeSet for sorted elements
        TreeSet<Integer> sortedNumbers = new TreeSet<>();
        sortedNumbers.add(5);
        sortedNumbers.add(2);
        sortedNumbers.add(8);
        sortedNumbers.add(1);

        // Navigation operations
        Integer first = sortedNumbers.first();
        Integer last = sortedNumbers.last();
        Integer lower = sortedNumbers.lower(5);
        Integer higher = sortedNumbers.higher(5);
        Integer floor = sortedNumbers.floor(4);
        Integer ceiling = sortedNumbers.ceiling(4);

        // Range operations
        SortedSet<Integer> headSet = sortedNumbers.headSet(5);
        SortedSet<Integer> tailSet = sortedNumbers.tailSet(3);
        SortedSet<Integer> subSet = sortedNumbers.subSet(2, 7);
    }
}
"""
    )

    parsers, queries = load_parsers()
    if "java" not in parsers:
        pytest.skip("Java parser not available")

    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=java_collections_project,
        parsers=parsers,
        queries=queries,
    )

    updater.run()

    class_calls = [
        call
        for call in mock_ingestor.ensure_node_batch.call_args_list
        if call[0][0] == "Class"
    ]

    created_classes = {call[0][1]["qualified_name"] for call in class_calls}
    project_name = java_collections_project.name

    expected_classes = {
        f"{project_name}.src.main.java.com.example.BasicCollections.BasicCollections",
    }

    missing_classes = expected_classes - created_classes
    assert not missing_classes, (
        f"Missing basic collections classes: {sorted(list(missing_classes))}"
    )


def test_custom_collection_implementations(
    java_collections_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test custom collection implementations."""
    test_file = (
        java_collections_project
        / "src"
        / "main"
        / "java"
        / "com"
        / "example"
        / "CustomCollections.java"
    )
    test_file.write_text(
        """
package com.example;

import java.util.*;

// Custom List implementation
public class ArrayStack<T> implements List<T> {
    private Object[] elements;
    private int size;
    private static final int DEFAULT_CAPACITY = 10;

    public ArrayStack() {
        elements = new Object[DEFAULT_CAPACITY];
        size = 0;
    }

    public void push(T element) {
        ensureCapacity();
        elements[size++] = element;
    }

    @SuppressWarnings("unchecked")
    public T pop() {
        if (isEmpty()) {
            throw new EmptyStackException();
        }
        T element = (T) elements[--size];
        elements[size] = null; // Prevent memory leak
        return element;
    }

    @SuppressWarnings("unchecked")
    public T peek() {
        if (isEmpty()) {
            throw new EmptyStackException();
        }
        return (T) elements[size - 1];
    }

    private void ensureCapacity() {
        if (size == elements.length) {
            elements = Arrays.copyOf(elements, elements.length * 2);
        }
    }

    @Override
    public int size() {
        return size;
    }

    @Override
    public boolean isEmpty() {
        return size == 0;
    }

    @Override
    public boolean contains(Object o) {
        for (int i = 0; i < size; i++) {
            if (Objects.equals(elements[i], o)) {
                return true;
            }
        }
        return false;
    }

    @Override
    public Iterator<T> iterator() {
        return new ArrayStackIterator();
    }

    @Override
    public Object[] toArray() {
        return Arrays.copyOf(elements, size);
    }

    @Override
    @SuppressWarnings("unchecked")
    public <U> U[] toArray(U[] a) {
        if (a.length < size) {
            return (U[]) Arrays.copyOf(elements, size, a.getClass());
        }
        System.arraycopy(elements, 0, a, 0, size);
        if (a.length > size) {
            a[size] = null;
        }
        return a;
    }

    @Override
    public boolean add(T t) {
        push(t);
        return true;
    }

    @Override
    public boolean remove(Object o) {
        for (int i = 0; i < size; i++) {
            if (Objects.equals(elements[i], o)) {
                System.arraycopy(elements, i + 1, elements, i, size - i - 1);
                size--;
                elements[size] = null;
                return true;
            }
        }
        return false;
    }

    @Override
    public boolean containsAll(Collection<?> c) {
        for (Object item : c) {
            if (!contains(item)) {
                return false;
            }
        }
        return true;
    }

    @Override
    public boolean addAll(Collection<? extends T> c) {
        boolean modified = false;
        for (T item : c) {
            add(item);
            modified = true;
        }
        return modified;
    }

    @Override
    public boolean addAll(int index, Collection<? extends T> c) {
        throw new UnsupportedOperationException("addAll at index not supported");
    }

    @Override
    public boolean removeAll(Collection<?> c) {
        boolean modified = false;
        for (Object item : c) {
            while (remove(item)) {
                modified = true;
            }
        }
        return modified;
    }

    @Override
    public boolean retainAll(Collection<?> c) {
        boolean modified = false;
        for (int i = size - 1; i >= 0; i--) {
            if (!c.contains(elements[i])) {
                remove(i);
                modified = true;
            }
        }
        return modified;
    }

    @Override
    public void clear() {
        for (int i = 0; i < size; i++) {
            elements[i] = null;
        }
        size = 0;
    }

    @Override
    @SuppressWarnings("unchecked")
    public T get(int index) {
        if (index < 0 || index >= size) {
            throw new IndexOutOfBoundsException("Index: " + index + ", Size: " + size);
        }
        return (T) elements[index];
    }

    @Override
    @SuppressWarnings("unchecked")
    public T set(int index, T element) {
        if (index < 0 || index >= size) {
            throw new IndexOutOfBoundsException("Index: " + index + ", Size: " + size);
        }
        T oldValue = (T) elements[index];
        elements[index] = element;
        return oldValue;
    }

    @Override
    public void add(int index, T element) {
        if (index < 0 || index > size) {
            throw new IndexOutOfBoundsException("Index: " + index + ", Size: " + size);
        }
        ensureCapacity();
        System.arraycopy(elements, index, elements, index + 1, size - index);
        elements[index] = element;
        size++;
    }

    @Override
    @SuppressWarnings("unchecked")
    public T remove(int index) {
        if (index < 0 || index >= size) {
            throw new IndexOutOfBoundsException("Index: " + index + ", Size: " + size);
        }
        T oldValue = (T) elements[index];
        System.arraycopy(elements, index + 1, elements, index, size - index - 1);
        size--;
        elements[size] = null;
        return oldValue;
    }

    @Override
    public int indexOf(Object o) {
        for (int i = 0; i < size; i++) {
            if (Objects.equals(elements[i], o)) {
                return i;
            }
        }
        return -1;
    }

    @Override
    public int lastIndexOf(Object o) {
        for (int i = size - 1; i >= 0; i--) {
            if (Objects.equals(elements[i], o)) {
                return i;
            }
        }
        return -1;
    }

    @Override
    public ListIterator<T> listIterator() {
        return new ArrayStackListIterator(0);
    }

    @Override
    public ListIterator<T> listIterator(int index) {
        return new ArrayStackListIterator(index);
    }

    @Override
    public List<T> subList(int fromIndex, int toIndex) {
        if (fromIndex < 0 || toIndex > size || fromIndex > toIndex) {
            throw new IndexOutOfBoundsException();
        }
        ArrayStack<T> subList = new ArrayStack<>();
        for (int i = fromIndex; i < toIndex; i++) {
            subList.add(get(i));
        }
        return subList;
    }

    private class ArrayStackIterator implements Iterator<T> {
        private int cursor = 0;
        private int lastRet = -1;

        @Override
        public boolean hasNext() {
            return cursor < size;
        }

        @Override
        @SuppressWarnings("unchecked")
        public T next() {
            if (cursor >= size) {
                throw new NoSuchElementException();
            }
            lastRet = cursor;
            return (T) elements[cursor++];
        }

        @Override
        public void remove() {
            if (lastRet < 0) {
                throw new IllegalStateException();
            }
            ArrayStack.this.remove(lastRet);
            cursor = lastRet;
            lastRet = -1;
        }
    }

    private class ArrayStackListIterator implements ListIterator<T> {
        private int cursor;
        private int lastRet = -1;

        ArrayStackListIterator(int index) {
            cursor = index;
        }

        @Override
        public boolean hasNext() {
            return cursor < size;
        }

        @Override
        @SuppressWarnings("unchecked")
        public T next() {
            if (cursor >= size) {
                throw new NoSuchElementException();
            }
            lastRet = cursor;
            return (T) elements[cursor++];
        }

        @Override
        public boolean hasPrevious() {
            return cursor > 0;
        }

        @Override
        @SuppressWarnings("unchecked")
        public T previous() {
            if (cursor <= 0) {
                throw new NoSuchElementException();
            }
            lastRet = --cursor;
            return (T) elements[cursor];
        }

        @Override
        public int nextIndex() {
            return cursor;
        }

        @Override
        public int previousIndex() {
            return cursor - 1;
        }

        @Override
        public void remove() {
            if (lastRet < 0) {
                throw new IllegalStateException();
            }
            ArrayStack.this.remove(lastRet);
            cursor = lastRet;
            lastRet = -1;
        }

        @Override
        public void set(T t) {
            if (lastRet < 0) {
                throw new IllegalStateException();
            }
            ArrayStack.this.set(lastRet, t);
        }

        @Override
        public void add(T t) {
            ArrayStack.this.add(cursor++, t);
            lastRet = -1;
        }
    }
}

// Custom Map implementation
class SimpleHashMap<K, V> implements Map<K, V> {
    private static final int DEFAULT_CAPACITY = 16;
    private static final double LOAD_FACTOR = 0.75;

    private Entry<K, V>[] buckets;
    private int size;
    private int threshold;

    @SuppressWarnings("unchecked")
    public SimpleHashMap() {
        buckets = new Entry[DEFAULT_CAPACITY];
        threshold = (int) (DEFAULT_CAPACITY * LOAD_FACTOR);
        size = 0;
    }

    private static class Entry<K, V> implements Map.Entry<K, V> {
        K key;
        V value;
        Entry<K, V> next;

        Entry(K key, V value) {
            this.key = key;
            this.value = value;
        }

        @Override
        public K getKey() {
            return key;
        }

        @Override
        public V getValue() {
            return value;
        }

        @Override
        public V setValue(V value) {
            V oldValue = this.value;
            this.value = value;
            return oldValue;
        }
    }

    private int hash(Object key) {
        if (key == null) return 0;
        return Math.abs(key.hashCode() % buckets.length);
    }

    @Override
    public V put(K key, V value) {
        int index = hash(key);
        Entry<K, V> entry = buckets[index];

        while (entry != null) {
            if (Objects.equals(entry.key, key)) {
                V oldValue = entry.value;
                entry.value = value;
                return oldValue;
            }
            entry = entry.next;
        }

        Entry<K, V> newEntry = new Entry<>(key, value);
        newEntry.next = buckets[index];
        buckets[index] = newEntry;
        size++;

        if (size > threshold) {
            resize();
        }

        return null;
    }

    @Override
    public V get(Object key) {
        int index = hash(key);
        Entry<K, V> entry = buckets[index];

        while (entry != null) {
            if (Objects.equals(entry.key, key)) {
                return entry.value;
            }
            entry = entry.next;
        }
        return null;
    }

    @Override
    public V remove(Object key) {
        int index = hash(key);
        Entry<K, V> entry = buckets[index];
        Entry<K, V> prev = null;

        while (entry != null) {
            if (Objects.equals(entry.key, key)) {
                if (prev == null) {
                    buckets[index] = entry.next;
                } else {
                    prev.next = entry.next;
                }
                size--;
                return entry.value;
            }
            prev = entry;
            entry = entry.next;
        }
        return null;
    }

    @SuppressWarnings("unchecked")
    private void resize() {
        Entry<K, V>[] oldBuckets = buckets;
        buckets = new Entry[oldBuckets.length * 2];
        threshold = (int) (buckets.length * LOAD_FACTOR);
        size = 0;

        for (Entry<K, V> head : oldBuckets) {
            Entry<K, V> entry = head;
            while (entry != null) {
                put(entry.key, entry.value);
                entry = entry.next;
            }
        }
    }

    @Override
    public int size() {
        return size;
    }

    @Override
    public boolean isEmpty() {
        return size == 0;
    }

    @Override
    public boolean containsKey(Object key) {
        return get(key) != null;
    }

    @Override
    public boolean containsValue(Object value) {
        for (Entry<K, V> head : buckets) {
            Entry<K, V> entry = head;
            while (entry != null) {
                if (Objects.equals(entry.value, value)) {
                    return true;
                }
                entry = entry.next;
            }
        }
        return false;
    }

    @Override
    public void putAll(Map<? extends K, ? extends V> m) {
        for (Map.Entry<? extends K, ? extends V> entry : m.entrySet()) {
            put(entry.getKey(), entry.getValue());
        }
    }

    @Override
    public void clear() {
        Arrays.fill(buckets, null);
        size = 0;
    }

    @Override
    public Set<K> keySet() {
        Set<K> keys = new HashSet<>();
        for (Entry<K, V> head : buckets) {
            Entry<K, V> entry = head;
            while (entry != null) {
                keys.add(entry.key);
                entry = entry.next;
            }
        }
        return keys;
    }

    @Override
    public Collection<V> values() {
        List<V> values = new ArrayList<>();
        for (Entry<K, V> head : buckets) {
            Entry<K, V> entry = head;
            while (entry != null) {
                values.add(entry.value);
                entry = entry.next;
            }
        }
        return values;
    }

    @Override
    public Set<Map.Entry<K, V>> entrySet() {
        Set<Map.Entry<K, V>> entries = new HashSet<>();
        for (Entry<K, V> head : buckets) {
            Entry<K, V> entry = head;
            while (entry != null) {
                entries.add(entry);
                entry = entry.next;
            }
        }
        return entries;
    }
}
"""
    )

    parsers, queries = load_parsers()
    if "java" not in parsers:
        pytest.skip("Java parser not available")

    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=java_collections_project,
        parsers=parsers,
        queries=queries,
    )

    updater.run()

    class_calls = [
        call
        for call in mock_ingestor.ensure_node_batch.call_args_list
        if call[0][0] == "Class"
    ]

    created_classes = {call[0][1]["qualified_name"] for call in class_calls}
    project_name = java_collections_project.name

    expected_classes = {
        f"{project_name}.src.main.java.com.example.CustomCollections.ArrayStack",
        f"{project_name}.src.main.java.com.example.CustomCollections.ArrayStack.ArrayStackIterator",
        f"{project_name}.src.main.java.com.example.CustomCollections.ArrayStack.ArrayStackListIterator",
        f"{project_name}.src.main.java.com.example.CustomCollections.SimpleHashMap",
        f"{project_name}.src.main.java.com.example.CustomCollections.SimpleHashMap.Entry",
    }

    missing_classes = expected_classes - created_classes
    assert not missing_classes, (
        f"Missing custom collection classes: {sorted(list(missing_classes))}"
    )


def test_iterator_patterns_enhanced_for(
    java_collections_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test iterator patterns and enhanced for loops."""
    test_file = (
        java_collections_project
        / "src"
        / "main"
        / "java"
        / "com"
        / "example"
        / "IteratorPatterns.java"
    )
    test_file.write_text(
        """
package com.example;

import java.util.*;

public class IteratorPatterns {

    public void demonstrateIterators() {
        List<String> fruits = Arrays.asList("apple", "banana", "cherry", "date");

        // Basic iterator
        Iterator<String> iterator = fruits.iterator();
        while (iterator.hasNext()) {
            String fruit = iterator.next();
            System.out.println(fruit);
        }

        // ListIterator for bidirectional traversal
        ListIterator<String> listIterator = fruits.listIterator();

        // Forward iteration
        while (listIterator.hasNext()) {
            System.out.println("Forward: " + listIterator.next());
        }

        // Backward iteration
        while (listIterator.hasPrevious()) {
            System.out.println("Backward: " + listIterator.previous());
        }

        // Iterator with removal
        List<Integer> numbers = new ArrayList<>(Arrays.asList(1, 2, 3, 4, 5, 6));
        Iterator<Integer> numIterator = numbers.iterator();
        while (numIterator.hasNext()) {
            Integer num = numIterator.next();
            if (num % 2 == 0) {
                numIterator.remove(); // Remove even numbers
            }
        }
    }

    public void demonstrateEnhancedForLoops() {
        // Enhanced for loop with List
        List<String> colors = Arrays.asList("red", "green", "blue", "yellow");
        for (String color : colors) {
            System.out.println("Color: " + color);
        }

        // Enhanced for loop with Set
        Set<Integer> uniqueNumbers = new HashSet<>(Arrays.asList(1, 2, 3, 2, 4, 1));
        for (Integer number : uniqueNumbers) {
            System.out.println("Unique number: " + number);
        }

        // Enhanced for loop with Map entries
        Map<String, Integer> ages = new HashMap<>();
        ages.put("Alice", 25);
        ages.put("Bob", 30);
        ages.put("Charlie", 35);

        for (Map.Entry<String, Integer> entry : ages.entrySet()) {
            System.out.println(entry.getKey() + " is " + entry.getValue() + " years old");
        }

        // Enhanced for loop with Map keys
        for (String name : ages.keySet()) {
            System.out.println("Name: " + name);
        }

        // Enhanced for loop with Map values
        for (Integer age : ages.values()) {
            System.out.println("Age: " + age);
        }

        // Enhanced for loop with arrays
        String[] languages = {"Java", "Python", "JavaScript", "Go"};
        for (String language : languages) {
            System.out.println("Language: " + language);
        }
    }

    public void demonstrateCustomIterable() {
        NumberRange range = new NumberRange(1, 5);

        // Can use enhanced for loop because NumberRange implements Iterable
        for (Integer number : range) {
            System.out.println("Number in range: " + number);
        }

        // Manual iterator usage
        Iterator<Integer> rangeIterator = range.iterator();
        while (rangeIterator.hasNext()) {
            System.out.println("Manual iteration: " + rangeIterator.next());
        }
    }

    public void demonstrateIteratorFailFast() {
        List<String> items = new ArrayList<>(Arrays.asList("a", "b", "c", "d"));

        try {
            Iterator<String> iterator = items.iterator();
            while (iterator.hasNext()) {
                String item = iterator.next();
                if ("b".equals(item)) {
                    items.add("e"); // This will cause ConcurrentModificationException
                }
            }
        } catch (ConcurrentModificationException e) {
            System.out.println("Caught concurrent modification exception");
        }

        // Safe way to modify collection during iteration
        items = new ArrayList<>(Arrays.asList("a", "b", "c", "d"));
        Iterator<String> safeIterator = items.iterator();
        while (safeIterator.hasNext()) {
            String item = safeIterator.next();
            if ("b".equals(item)) {
                safeIterator.remove(); // Safe removal using iterator
            }
        }
    }
}

// Custom iterable class
class NumberRange implements Iterable<Integer> {
    private final int start;
    private final int end;

    public NumberRange(int start, int end) {
        this.start = start;
        this.end = end;
    }

    @Override
    public Iterator<Integer> iterator() {
        return new NumberRangeIterator();
    }

    private class NumberRangeIterator implements Iterator<Integer> {
        private int current = start;

        @Override
        public boolean hasNext() {
            return current <= end;
        }

        @Override
        public Integer next() {
            if (!hasNext()) {
                throw new NoSuchElementException();
            }
            return current++;
        }

        @Override
        public void remove() {
            throw new UnsupportedOperationException("Remove not supported");
        }
    }
}

// Fibonacci iterator
class FibonacciIterable implements Iterable<Long> {
    private final int maxCount;

    public FibonacciIterable(int maxCount) {
        this.maxCount = maxCount;
    }

    @Override
    public Iterator<Long> iterator() {
        return new FibonacciIterator();
    }

    private class FibonacciIterator implements Iterator<Long> {
        private long previous = 0;
        private long current = 1;
        private int count = 0;

        @Override
        public boolean hasNext() {
            return count < maxCount;
        }

        @Override
        public Long next() {
            if (!hasNext()) {
                throw new NoSuchElementException();
            }

            if (count == 0) {
                count++;
                return previous;
            } else if (count == 1) {
                count++;
                return current;
            } else {
                long next = previous + current;
                previous = current;
                current = next;
                count++;
                return next;
            }
        }
    }
}
"""
    )

    parsers, queries = load_parsers()
    if "java" not in parsers:
        pytest.skip("Java parser not available")

    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=java_collections_project,
        parsers=parsers,
        queries=queries,
    )

    updater.run()

    class_calls = [
        call
        for call in mock_ingestor.ensure_node_batch.call_args_list
        if call[0][0] == "Class"
    ]

    created_classes = {call[0][1]["qualified_name"] for call in class_calls}
    project_name = java_collections_project.name

    expected_classes = {
        f"{project_name}.src.main.java.com.example.IteratorPatterns.IteratorPatterns",
        f"{project_name}.src.main.java.com.example.IteratorPatterns.NumberRange",
        f"{project_name}.src.main.java.com.example.IteratorPatterns.NumberRange.NumberRangeIterator",
        f"{project_name}.src.main.java.com.example.IteratorPatterns.FibonacciIterable",
        f"{project_name}.src.main.java.com.example.IteratorPatterns.FibonacciIterable.FibonacciIterator",
    }

    missing_classes = expected_classes - created_classes
    assert not missing_classes, (
        f"Missing iterator pattern classes: {sorted(list(missing_classes))}"
    )


def test_map_operations_key_value_handling(
    java_collections_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test comprehensive map operations and key-value handling."""
    test_file = (
        java_collections_project
        / "src"
        / "main"
        / "java"
        / "com"
        / "example"
        / "MapOperations.java"
    )
    test_file.write_text(
        """
package com.example;

import java.util.*;
import java.util.function.*;

public class MapOperations {

    public void demonstrateMapBasics() {
        Map<String, Integer> wordCounts = new HashMap<>();

        // Basic put operations
        wordCounts.put("hello", 5);
        wordCounts.put("world", 3);
        wordCounts.put("java", 8);

        // Conditional put operations
        wordCounts.putIfAbsent("python", 2);
        wordCounts.putIfAbsent("java", 10); // Won't overwrite existing value

        // Get operations with defaults
        Integer count = wordCounts.get("hello");
        Integer defaultCount = wordCounts.getOrDefault("ruby", 0);

        // Compute operations
        wordCounts.compute("scala", (key, val) -> val == null ? 1 : val + 1);
        wordCounts.computeIfAbsent("kotlin", key -> key.length());
        wordCounts.computeIfPresent("java", (key, val) -> val * 2);

        // Merge operation
        wordCounts.merge("go", 1, Integer::sum);
        wordCounts.merge("java", 5, Integer::sum);
    }

    public void demonstrateMapIteration() {
        Map<String, List<String>> categories = new HashMap<>();
        categories.put("fruits", Arrays.asList("apple", "banana", "orange"));
        categories.put("vegetables", Arrays.asList("carrot", "broccoli", "spinach"));
        categories.put("grains", Arrays.asList("rice", "wheat", "oats"));

        // Iterate over entries
        for (Map.Entry<String, List<String>> entry : categories.entrySet()) {
            String category = entry.getKey();
            List<String> items = entry.getValue();
            System.out.println(category + ": " + items);
        }

        // Iterate using forEach (Java 8+)
        categories.forEach((category, items) -> {
            System.out.println("Category: " + category);
            items.forEach(item -> System.out.println("  - " + item));
        });

        // Iterate over keys and values separately
        for (String category : categories.keySet()) {
            System.out.println("Processing category: " + category);
        }

        for (List<String> items : categories.values()) {
            System.out.println("Items count: " + items.size());
        }
    }

    public void demonstrateMapTransformations() {
        Map<String, Integer> scores = new HashMap<>();
        scores.put("Alice", 85);
        scores.put("Bob", 92);
        scores.put("Charlie", 78);
        scores.put("Diana", 96);

        // Replace all values based on condition
        scores.replaceAll((name, score) -> score < 80 ? score + 5 : score);

        // Filter and collect to new map
        Map<String, Integer> highScores = new HashMap<>();
        scores.entrySet().stream()
            .filter(entry -> entry.getValue() >= 90)
            .forEach(entry -> highScores.put(entry.getKey(), entry.getValue()));

        // Transform values while keeping keys
        Map<String, String> grades = new HashMap<>();
        scores.forEach((name, score) -> {
            String grade = getGrade(score);
            grades.put(name, grade);
        });
    }

    private String getGrade(int score) {
        if (score >= 90) return "A";
        if (score >= 80) return "B";
        if (score >= 70) return "C";
        if (score >= 60) return "D";
        return "F";
    }

    public void demonstrateNestedMaps() {
        // Nested map structure: Country -> City -> Population
        Map<String, Map<String, Integer>> countryData = new HashMap<>();

        Map<String, Integer> usaCities = new HashMap<>();
        usaCities.put("New York", 8_400_000);
        usaCities.put("Los Angeles", 3_900_000);
        usaCities.put("Chicago", 2_700_000);
        countryData.put("USA", usaCities);

        Map<String, Integer> ukCities = new HashMap<>();
        ukCities.put("London", 8_900_000);
        ukCities.put("Birmingham", 1_100_000);
        ukCities.put("Manchester", 550_000);
        countryData.put("UK", ukCities);

        // Access nested data
        Integer londonPop = countryData.get("UK").get("London");

        // Safe access with null checks
        Integer parisPopulation = Optional.ofNullable(countryData.get("France"))
            .map(cities -> cities.get("Paris"))
            .orElse(0);

        // Add to nested structure
        countryData.computeIfAbsent("Canada", k -> new HashMap<>())
                   .put("Toronto", 2_930_000);

        // Iterate through nested structure
        countryData.forEach((country, cities) -> {
            System.out.println("Country: " + country);
            cities.forEach((city, population) -> {
                System.out.println("  " + city + ": " + population);
            });
        });
    }

    public void demonstrateMapWithCustomObjects() {
        Map<Person, String> personRoles = new HashMap<>();

        Person alice = new Person("Alice", 30);
        Person bob = new Person("Bob", 25);
        Person charlie = new Person("Charlie", 35);

        personRoles.put(alice, "Manager");
        personRoles.put(bob, "Developer");
        personRoles.put(charlie, "Analyst");

        // Lookup by person object
        String aliceRole = personRoles.get(alice);

        // Use TreeMap for sorted keys
        Map<Person, String> sortedPersonRoles = new TreeMap<>(personRoles);

        // Group by age ranges
        Map<String, List<Person>> ageGroups = new HashMap<>();
        personRoles.keySet().forEach(person -> {
            String ageGroup = getAgeGroup(person.getAge());
            ageGroups.computeIfAbsent(ageGroup, k -> new ArrayList<>()).add(person);
        });
    }

    private String getAgeGroup(int age) {
        if (age < 30) return "20s";
        if (age < 40) return "30s";
        return "40+";
    }
}

class Person implements Comparable<Person> {
    private String name;
    private int age;

    public Person(String name, int age) {
        this.name = name;
        this.age = age;
    }

    public String getName() { return name; }
    public int getAge() { return age; }

    @Override
    public boolean equals(Object o) {
        if (this == o) return true;
        if (o == null || getClass() != o.getClass()) return false;
        Person person = (Person) o;
        return age == person.age && Objects.equals(name, person.name);
    }

    @Override
    public int hashCode() {
        return Objects.hash(name, age);
    }

    @Override
    public int compareTo(Person other) {
        int nameComparison = this.name.compareTo(other.name);
        if (nameComparison != 0) {
            return nameComparison;
        }
        return Integer.compare(this.age, other.age);
    }

    @Override
    public String toString() {
        return name + " (" + age + ")";
    }
}
"""
    )

    parsers, queries = load_parsers()
    if "java" not in parsers:
        pytest.skip("Java parser not available")

    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=java_collections_project,
        parsers=parsers,
        queries=queries,
    )

    updater.run()

    class_calls = [
        call
        for call in mock_ingestor.ensure_node_batch.call_args_list
        if call[0][0] == "Class"
    ]

    created_classes = {call[0][1]["qualified_name"] for call in class_calls}
    project_name = java_collections_project.name

    expected_classes = {
        f"{project_name}.src.main.java.com.example.MapOperations.MapOperations",
        f"{project_name}.src.main.java.com.example.MapOperations.Person",
    }

    missing_classes = expected_classes - created_classes
    assert not missing_classes, (
        f"Missing map operations classes: {sorted(list(missing_classes))}"
    )


def test_set_operations_uniqueness(
    java_collections_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test set operations and uniqueness constraints."""
    test_file = (
        java_collections_project
        / "src"
        / "main"
        / "java"
        / "com"
        / "example"
        / "SetOperations.java"
    )
    test_file.write_text(
        """
package com.example;

import java.util.*;

public class SetOperations {

    public void demonstrateSetBasics() {
        // HashSet - no ordering
        Set<String> hashSet = new HashSet<>();
        hashSet.add("banana");
        hashSet.add("apple");
        hashSet.add("cherry");
        hashSet.add("apple"); // Duplicate, won't be added

        // TreeSet - sorted ordering
        Set<String> treeSet = new TreeSet<>();
        treeSet.add("banana");
        treeSet.add("apple");
        treeSet.add("cherry");

        // LinkedHashSet - insertion ordering
        Set<String> linkedSet = new LinkedHashSet<>();
        linkedSet.add("banana");
        linkedSet.add("apple");
        linkedSet.add("cherry");

        System.out.println("HashSet: " + hashSet);
        System.out.println("TreeSet: " + treeSet);
        System.out.println("LinkedHashSet: " + linkedSet);
    }

    public void demonstrateSetOperations() {
        Set<Integer> set1 = new HashSet<>(Arrays.asList(1, 2, 3, 4, 5));
        Set<Integer> set2 = new HashSet<>(Arrays.asList(4, 5, 6, 7, 8));

        // Union
        Set<Integer> union = new HashSet<>(set1);
        union.addAll(set2);
        System.out.println("Union: " + union);

        // Intersection
        Set<Integer> intersection = new HashSet<>(set1);
        intersection.retainAll(set2);
        System.out.println("Intersection: " + intersection);

        // Difference (set1 - set2)
        Set<Integer> difference = new HashSet<>(set1);
        difference.removeAll(set2);
        System.out.println("Difference: " + difference);

        // Symmetric difference
        Set<Integer> symmetricDiff = new HashSet<>(union);
        symmetricDiff.removeAll(intersection);
        System.out.println("Symmetric Difference: " + symmetricDiff);

        // Subset check
        Set<Integer> subset = new HashSet<>(Arrays.asList(2, 3));
        boolean isSubset = set1.containsAll(subset);
        System.out.println("Is subset: " + isSubset);

        // Disjoint check
        Set<Integer> disjointSet = new HashSet<>(Arrays.asList(9, 10, 11));
        boolean areDisjoint = Collections.disjoint(set1, disjointSet);
        System.out.println("Are disjoint: " + areDisjoint);
    }

    public void demonstrateCustomObjectSets() {
        Set<Student> students = new HashSet<>();
        students.add(new Student("Alice", 123));
        students.add(new Student("Bob", 456));
        students.add(new Student("Alice", 123)); // Duplicate based on equals/hashCode

        System.out.println("Number of unique students: " + students.size());

        // TreeSet with custom objects (requires Comparable or Comparator)
        Set<Student> sortedStudents = new TreeSet<>(students);
        System.out.println("Sorted students: " + sortedStudents);

        // Custom comparator for different sorting
        Set<Student> studentsByName = new TreeSet<>(
            Comparator.comparing(Student::getName).thenComparingInt(Student::getId)
        );
        studentsByName.addAll(students);

        // EnumSet for enum types
        Set<Priority> priorities = EnumSet.of(Priority.HIGH, Priority.MEDIUM);
        priorities.add(Priority.LOW);
        System.out.println("Priorities: " + priorities);

        // EnumSet all values
        Set<Priority> allPriorities = EnumSet.allOf(Priority.class);

        // EnumSet range
        Set<Priority> mediumToHigh = EnumSet.range(Priority.MEDIUM, Priority.HIGH);
    }

    public void demonstrateSetUtilities() {
        Set<String> fruits = new HashSet<>(Arrays.asList("apple", "banana", "cherry"));
        Set<String> vegetables = new HashSet<>(Arrays.asList("carrot", "broccoli", "spinach"));

        // Immutable sets
        Set<String> immutableFruits = Collections.unmodifiableSet(fruits);

        // Singleton set
        Set<String> singletonSet = Collections.singleton("only-item");

        // Empty set
        Set<String> emptySet = Collections.emptySet();

        // Convert between collections
        List<String> fruitList = new ArrayList<>(fruits);
        Set<String> backToSet = new HashSet<>(fruitList);

        // Check if set is modified
        boolean added = fruits.add("orange");
        boolean removed = fruits.remove("banana");

        // Bulk operations
        Set<String> itemsToRemove = Arrays.asList("apple", "grape").stream()
            .collect(HashSet::new, HashSet::add, HashSet::addAll);
        fruits.removeAll(itemsToRemove);

        // Keep only specific items
        Set<String> itemsToKeep = new HashSet<>(Arrays.asList("cherry", "orange"));
        fruits.retainAll(itemsToKeep);
    }

    public void demonstrateNavigableSet() {
        NavigableSet<Integer> scores = new TreeSet<>(Arrays.asList(85, 92, 78, 96, 88, 75, 94));

        // Navigation operations
        Integer lowest = scores.first();
        Integer highest = scores.last();

        Integer lowerThan90 = scores.lower(90);  // Highest < 90
        Integer floorOf90 = scores.floor(90);    // Highest <= 90
        Integer ceilingOf90 = scores.ceiling(90); // Lowest >= 90
        Integer higherThan90 = scores.higher(90); // Lowest > 90

        // Poll operations (remove and return)
        Integer removedFirst = scores.pollFirst();
        Integer removedLast = scores.pollLast();

        // Subset operations
        NavigableSet<Integer> passingScores = scores.tailSet(80, true);
        NavigableSet<Integer> excellentScores = scores.headSet(95, false);
        NavigableSet<Integer> midRangeScores = scores.subSet(80, true, 95, false);

        // Descending view
        NavigableSet<Integer> descendingScores = scores.descendingSet();
        Iterator<Integer> descendingIterator = scores.descendingIterator();

        while (descendingIterator.hasNext()) {
            System.out.println("Descending: " + descendingIterator.next());
        }
    }
}

class Student implements Comparable<Student> {
    private String name;
    private int id;

    public Student(String name, int id) {
        this.name = name;
        this.id = id;
    }

    public String getName() { return name; }
    public int getId() { return id; }

    @Override
    public boolean equals(Object o) {
        if (this == o) return true;
        if (o == null || getClass() != o.getClass()) return false;
        Student student = (Student) o;
        return id == student.id && Objects.equals(name, student.name);
    }

    @Override
    public int hashCode() {
        return Objects.hash(name, id);
    }

    @Override
    public int compareTo(Student other) {
        int nameComparison = this.name.compareTo(other.name);
        if (nameComparison != 0) {
            return nameComparison;
        }
        return Integer.compare(this.id, other.id);
    }

    @Override
    public String toString() {
        return name + " (" + id + ")";
    }
}

enum Priority {
    LOW, MEDIUM, HIGH
}
"""
    )

    parsers, queries = load_parsers()
    if "java" not in parsers:
        pytest.skip("Java parser not available")

    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=java_collections_project,
        parsers=parsers,
        queries=queries,
    )

    updater.run()

    all_calls = mock_ingestor.ensure_node_batch.call_args_list
    class_calls = [call for call in all_calls if call[0][0] == "Class"]
    enum_calls = [call for call in all_calls if call[0][0] == "Enum"]

    created_classes = {call[0][1]["qualified_name"] for call in class_calls}
    created_enums = {call[0][1]["qualified_name"] for call in enum_calls}

    project_name = java_collections_project.name
    expected_classes = {
        f"{project_name}.src.main.java.com.example.SetOperations.SetOperations",
        f"{project_name}.src.main.java.com.example.SetOperations.Student",
    }

    expected_enums = {
        f"{project_name}.src.main.java.com.example.SetOperations.Priority",
    }

    missing_classes = expected_classes - created_classes
    missing_enums = expected_enums - created_enums

    assert not missing_classes, (
        f"Missing set operations classes: {sorted(list(missing_classes))}"
    )
    assert not missing_enums, (
        f"Missing set operations enums: {sorted(list(missing_enums))}"
    )


def test_stream_api_integration_collections(
    java_collections_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test Stream API integration with collections."""
    test_file = (
        java_collections_project
        / "src"
        / "main"
        / "java"
        / "com"
        / "example"
        / "StreamCollections.java"
    )
    test_file.write_text(
        """
package com.example;

import java.util.*;
import java.util.stream.*;

public class StreamCollections {

    public void demonstrateStreamBasics() {
        List<String> words = Arrays.asList("hello", "world", "java", "stream", "api");

        // Basic stream operations
        List<String> upperCaseWords = words.stream()
            .map(String::toUpperCase)
            .collect(Collectors.toList());

        List<String> longWords = words.stream()
            .filter(word -> word.length() > 4)
            .collect(Collectors.toList());

        // Stream to different collection types
        Set<String> uniqueWords = words.stream()
            .collect(Collectors.toSet());

        LinkedList<String> linkedWords = words.stream()
            .collect(Collectors.toCollection(LinkedList::new));

        TreeSet<String> sortedWords = words.stream()
            .collect(Collectors.toCollection(TreeSet::new));
    }

    public void demonstrateCollectorsToMap() {
        List<Book> books = Arrays.asList(
            new Book("1984", "George Orwell", 1949, 328),
            new Book("To Kill a Mockingbird", "Harper Lee", 1960, 376),
            new Book("The Great Gatsby", "F. Scott Fitzgerald", 1925, 180),
            new Book("Pride and Prejudice", "Jane Austen", 1813, 432)
        );

        // Collect to Map with custom key and value
        Map<String, Integer> titleToYear = books.stream()
            .collect(Collectors.toMap(Book::getTitle, Book::getYear));

        Map<String, Book> authorToBook = books.stream()
            .collect(Collectors.toMap(Book::getAuthor, book -> book));

        // Handle duplicate keys
        Map<Integer, String> yearToTitle = books.stream()
            .collect(Collectors.toMap(
                Book::getYear,
                Book::getTitle,
                (existing, replacement) -> existing + ", " + replacement
            ));

        // Collect to specific Map implementation
        TreeMap<String, Integer> sortedTitleToPages = books.stream()
            .collect(Collectors.toMap(
                Book::getTitle,
                Book::getPages,
                (a, b) -> a,
                TreeMap::new
            ));
    }

    public void demonstrateGroupingCollectors() {
        List<Employee> employees = Arrays.asList(
            new Employee("Alice", "Engineering", 85000),
            new Employee("Bob", "Engineering", 90000),
            new Employee("Charlie", "Marketing", 75000),
            new Employee("Diana", "Engineering", 95000),
            new Employee("Eve", "Marketing", 80000),
            new Employee("Frank", "Sales", 70000)
        );

        // Group by department
        Map<String, List<Employee>> byDepartment = employees.stream()
            .collect(Collectors.groupingBy(Employee::getDepartment));

        // Group by department and count
        Map<String, Long> countByDepartment = employees.stream()
            .collect(Collectors.groupingBy(
                Employee::getDepartment,
                Collectors.counting()
            ));

        // Group by department and calculate average salary
        Map<String, Double> avgSalaryByDept = employees.stream()
            .collect(Collectors.groupingBy(
                Employee::getDepartment,
                Collectors.averagingDouble(Employee::getSalary)
            ));

        // Group by salary range
        Map<String, List<Employee>> bySalaryRange = employees.stream()
            .collect(Collectors.groupingBy(emp -> {
                if (emp.getSalary() < 75000) return "Low";
                if (emp.getSalary() < 90000) return "Medium";
                return "High";
            }));

        // Multi-level grouping
        Map<String, Map<String, List<Employee>>> byDeptAndSalaryRange = employees.stream()
            .collect(Collectors.groupingBy(
                Employee::getDepartment,
                Collectors.groupingBy(emp -> {
                    if (emp.getSalary() < 80000) return "Junior";
                    return "Senior";
                })
            ));
    }

    public void demonstratePartitioning() {
        List<Integer> numbers = Arrays.asList(1, 2, 3, 4, 5, 6, 7, 8, 9, 10);

        // Partition by even/odd
        Map<Boolean, List<Integer>> evenOdd = numbers.stream()
            .collect(Collectors.partitioningBy(n -> n % 2 == 0));

        List<Integer> evenNumbers = evenOdd.get(true);
        List<Integer> oddNumbers = evenOdd.get(false);

        // Partition and count
        Map<Boolean, Long> evenOddCount = numbers.stream()
            .collect(Collectors.partitioningBy(
                n -> n % 2 == 0,
                Collectors.counting()
            ));

        // Partition and find max
        Map<Boolean, Optional<Integer>> evenOddMax = numbers.stream()
            .collect(Collectors.partitioningBy(
                n -> n % 2 == 0,
                Collectors.maxBy(Integer::compareTo)
            ));
    }

    public void demonstrateAdvancedCollectors() {
        List<String> sentences = Arrays.asList(
            "The quick brown fox",
            "jumps over the lazy dog",
            "Java streams are powerful",
            "Collections framework is useful"
        );

        // Join strings
        String joined = sentences.stream()
            .collect(Collectors.joining(" | "));

        String joinedWithPrefixSuffix = sentences.stream()
            .collect(Collectors.joining(", ", "[", "]"));

        // Summarizing statistics
        List<Integer> lengths = sentences.stream()
            .mapToInt(String::length)
            .boxed()
            .collect(Collectors.toList());

        IntSummaryStatistics stats = sentences.stream()
            .collect(Collectors.summarizingInt(String::length));

        System.out.println("Count: " + stats.getCount());
        System.out.println("Sum: " + stats.getSum());
        System.out.println("Average: " + stats.getAverage());
        System.out.println("Min: " + stats.getMin());
        System.out.println("Max: " + stats.getMax());

        // Custom collector
        String concatenated = sentences.stream()
            .collect(Collector.of(
                StringBuilder::new,
                (sb, str) -> sb.append(str).append(" "),
                StringBuilder::append,
                StringBuilder::toString
            ));
    }

    public void demonstrateParallelStreams() {
        List<Integer> largeList = IntStream.range(1, 1000000)
            .boxed()
            .collect(Collectors.toList());

        // Sequential processing
        long sequentialSum = largeList.stream()
            .mapToInt(Integer::intValue)
            .sum();

        // Parallel processing
        long parallelSum = largeList.parallelStream()
            .mapToInt(Integer::intValue)
            .sum();

        // Parallel grouping
        Map<Integer, List<Integer>> parallelGrouped = largeList.parallelStream()
            .collect(Collectors.groupingBy(n -> n % 10));

        // Custom parallel reduction
        Optional<Integer> parallelMax = largeList.parallelStream()
            .reduce(Integer::max);
    }
}

class Book {
    private String title;
    private String author;
    private int year;
    private int pages;

    public Book(String title, String author, int year, int pages) {
        this.title = title;
        this.author = author;
        this.year = year;
        this.pages = pages;
    }

    public String getTitle() { return title; }
    public String getAuthor() { return author; }
    public int getYear() { return year; }
    public int getPages() { return pages; }

    @Override
    public String toString() {
        return title + " by " + author + " (" + year + ")";
    }
}

class Employee {
    private String name;
    private String department;
    private double salary;

    public Employee(String name, String department, double salary) {
        this.name = name;
        this.department = department;
        this.salary = salary;
    }

    public String getName() { return name; }
    public String getDepartment() { return department; }
    public double getSalary() { return salary; }

    @Override
    public String toString() {
        return name + " (" + department + ", $" + salary + ")";
    }
}
"""
    )

    parsers, queries = load_parsers()
    if "java" not in parsers:
        pytest.skip("Java parser not available")

    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=java_collections_project,
        parsers=parsers,
        queries=queries,
    )

    updater.run()

    class_calls = [
        call
        for call in mock_ingestor.ensure_node_batch.call_args_list
        if call[0][0] == "Class"
    ]

    created_classes = {call[0][1]["qualified_name"] for call in class_calls}
    project_name = java_collections_project.name

    expected_classes = {
        f"{project_name}.src.main.java.com.example.StreamCollections.StreamCollections",
        f"{project_name}.src.main.java.com.example.StreamCollections.Book",
        f"{project_name}.src.main.java.com.example.StreamCollections.Employee",
    }

    missing_classes = expected_classes - created_classes
    assert not missing_classes, (
        f"Missing stream collections classes: {sorted(list(missing_classes))}"
    )


def test_thread_safe_collections(
    java_collections_project: Path,
    mock_ingestor: MagicMock,
) -> None:
    """Test thread-safe collections like ConcurrentHashMap."""
    test_file = (
        java_collections_project
        / "src"
        / "main"
        / "java"
        / "com"
        / "example"
        / "ThreadSafeCollections.java"
    )
    test_file.write_text(
        """
package com.example;

import java.util.*;
import java.util.concurrent.*;

public class ThreadSafeCollections {

    public void demonstrateConcurrentHashMap() {
        ConcurrentHashMap<String, Integer> concurrentMap = new ConcurrentHashMap<>();

        // Thread-safe put operations
        concurrentMap.put("counter1", 0);
        concurrentMap.put("counter2", 0);

        // Atomic operations
        concurrentMap.putIfAbsent("counter3", 0);

        // Compute operations (atomic)
        concurrentMap.compute("counter1", (key, val) -> val == null ? 1 : val + 1);
        concurrentMap.computeIfAbsent("counter4", key -> 0);
        concurrentMap.computeIfPresent("counter2", (key, val) -> val + 10);

        // Merge operation (atomic)
        concurrentMap.merge("counter1", 5, Integer::sum);

        // Replace operations
        concurrentMap.replace("counter2", 10, 20); // Replace only if current value is 10
        concurrentMap.replace("counter3", 100); // Replace with new value

        // Bulk operations
        concurrentMap.forEach((key, value) -> {
            System.out.println(key + ": " + value);
        });

        // Search operations
        String foundKey = concurrentMap.search(1, (key, value) ->
            value > 5 ? key : null);

        // Reduce operations
        Integer sum = concurrentMap.reduce(1,
            (key, value) -> value,
            Integer::sum);
    }

    public void demonstrateConcurrentCollections() {
        // ConcurrentLinkedQueue - thread-safe queue
        ConcurrentLinkedQueue<String> queue = new ConcurrentLinkedQueue<>();
        queue.offer("item1");
        queue.offer("item2");
        String head = queue.poll();
        String peek = queue.peek();

        // ConcurrentLinkedDeque - thread-safe double-ended queue
        ConcurrentLinkedDeque<Integer> deque = new ConcurrentLinkedDeque<>();
        deque.addFirst(1);
        deque.addLast(2);
        Integer first = deque.removeFirst();
        Integer last = deque.removeLast();

        // ConcurrentSkipListMap - thread-safe sorted map
        ConcurrentSkipListMap<String, String> skipListMap = new ConcurrentSkipListMap<>();
        skipListMap.put("key3", "value3");
        skipListMap.put("key1", "value1");
        skipListMap.put("key2", "value2");

        // Navigation operations
        String firstKey = skipListMap.firstKey();
        String lastKey = skipListMap.lastKey();

        // ConcurrentSkipListSet - thread-safe sorted set
        ConcurrentSkipListSet<Integer> skipListSet = new ConcurrentSkipListSet<>();
        skipListSet.add(3);
        skipListSet.add(1);
        skipListSet.add(2);

        Integer firstElement = skipListSet.first();
        Integer lastElement = skipListSet.last();
    }

    public void demonstrateBlockingQueues() {
        // ArrayBlockingQueue - bounded blocking queue
        BlockingQueue<String> boundedQueue = new ArrayBlockingQueue<>(10);

        try {
            boundedQueue.put("item1"); // Blocks if queue is full
            String item = boundedQueue.take(); // Blocks if queue is empty

            // Non-blocking variants
            boolean offered = boundedQueue.offer("item2");
            String polled = boundedQueue.poll();

            // Timeout variants
            boolean offeredWithTimeout = boundedQueue.offer("item3", 1, TimeUnit.SECONDS);
            String polledWithTimeout = boundedQueue.poll(1, TimeUnit.SECONDS);
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
        }

        // LinkedBlockingQueue - optionally bounded
        BlockingQueue<Integer> linkedQueue = new LinkedBlockingQueue<>();

        // PriorityBlockingQueue - unbounded priority queue
        BlockingQueue<Task> priorityQueue = new PriorityBlockingQueue<>();
        priorityQueue.offer(new Task("Low priority task", 3));
        priorityQueue.offer(new Task("High priority task", 1));
        priorityQueue.offer(new Task("Medium priority task", 2));

        // DelayQueue - elements can only be taken when their delay has expired
        DelayQueue<DelayedTask> delayQueue = new DelayQueue<>();
        delayQueue.offer(new DelayedTask("Task 1", 1000)); // 1 second delay
        delayQueue.offer(new DelayedTask("Task 2", 2000)); // 2 second delay

        // SynchronousQueue - hands off between threads
        SynchronousQueue<String> synchronousQueue = new SynchronousQueue<>();
    }

    public void demonstrateSynchronizedCollections() {
        // Synchronized wrappers for legacy collections
        List<String> list = new ArrayList<>();
        List<String> synchronizedList = Collections.synchronizedList(list);

        Map<String, Integer> map = new HashMap<>();
        Map<String, Integer> synchronizedMap = Collections.synchronizedMap(map);

        Set<String> set = new HashSet<>();
        Set<String> synchronizedSet = Collections.synchronizedSet(set);

        // Important: Manual synchronization needed for iteration
        synchronized (synchronizedList) {
            for (String item : synchronizedList) {
                System.out.println(item);
            }
        }

        synchronized (synchronizedMap) {
            for (Map.Entry<String, Integer> entry : synchronizedMap.entrySet()) {
                System.out.println(entry.getKey() + ": " + entry.getValue());
            }
        }
    }

    public void demonstrateCopyOnWriteCollections() {
        // CopyOnWriteArrayList - thread-safe list with copy-on-write semantics
        CopyOnWriteArrayList<String> cowList = new CopyOnWriteArrayList<>();
        cowList.add("item1");
        cowList.add("item2");

        // Iterators are snapshot-based and don't throw ConcurrentModificationException
        for (String item : cowList) {
            System.out.println(item);
            // Safe to modify during iteration
            cowList.add("new item");
        }

        // CopyOnWriteArraySet - thread-safe set with copy-on-write semantics
        CopyOnWriteArraySet<Integer> cowSet = new CopyOnWriteArraySet<>();
        cowSet.add(1);
        cowSet.add(2);
        cowSet.add(1); // Duplicate, won't be added

        for (Integer number : cowSet) {
            System.out.println(number);
            cowSet.add(number + 10); // Safe modification during iteration
        }
    }

    public void demonstrateAtomicCollectionOperations() {
        // Using atomic operations with concurrent collections
        ConcurrentHashMap<String, AtomicInteger> counters = new ConcurrentHashMap<>();

        // Initialize counters
        counters.put("requests", new AtomicInteger(0));
        counters.put("errors", new AtomicInteger(0));
        counters.put("successes", new AtomicInteger(0));

        // Atomic increment operations
        counters.get("requests").incrementAndGet();
        counters.get("successes").incrementAndGet();

        // Atomic update operations
        counters.get("requests").updateAndGet(val -> val + 10);

        // Compare and set operations
        AtomicInteger errorCounter = counters.get("errors");
        errorCounter.compareAndSet(0, 1);

        // Get current values atomically
        int totalRequests = counters.get("requests").get();
        int totalErrors = counters.get("errors").get();
        int totalSuccesses = counters.get("successes").get();

        System.out.println("Requests: " + totalRequests);
        System.out.println("Errors: " + totalErrors);
        System.out.println("Successes: " + totalSuccesses);
    }
}

class Task implements Comparable<Task> {
    private String name;
    private int priority;

    public Task(String name, int priority) {
        this.name = name;
        this.priority = priority;
    }

    public String getName() { return name; }
    public int getPriority() { return priority; }

    @Override
    public int compareTo(Task other) {
        return Integer.compare(this.priority, other.priority);
    }

    @Override
    public String toString() {
        return name + " (priority: " + priority + ")";
    }
}

class DelayedTask implements Delayed {
    private String name;
    private long delayTime;

    public DelayedTask(String name, long delayMillis) {
        this.name = name;
        this.delayTime = System.currentTimeMillis() + delayMillis;
    }

    @Override
    public long getDelay(TimeUnit unit) {
        long remaining = delayTime - System.currentTimeMillis();
        return unit.convert(remaining, TimeUnit.MILLISECONDS);
    }

    @Override
    public int compareTo(Delayed other) {
        return Long.compare(this.getDelay(TimeUnit.MILLISECONDS),
                           other.getDelay(TimeUnit.MILLISECONDS));
    }

    public String getName() { return name; }

    @Override
    public String toString() {
        return name + " (delay: " + getDelay(TimeUnit.MILLISECONDS) + "ms)";
    }
}
"""
    )

    parsers, queries = load_parsers()
    if "java" not in parsers:
        pytest.skip("Java parser not available")

    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=java_collections_project,
        parsers=parsers,
        queries=queries,
    )

    updater.run()

    class_calls = [
        call
        for call in mock_ingestor.ensure_node_batch.call_args_list
        if call[0][0] == "Class"
    ]

    created_classes = {call[0][1]["qualified_name"] for call in class_calls}
    project_name = java_collections_project.name

    expected_classes = {
        f"{project_name}.src.main.java.com.example.ThreadSafeCollections.ThreadSafeCollections",
        f"{project_name}.src.main.java.com.example.ThreadSafeCollections.Task",
        f"{project_name}.src.main.java.com.example.ThreadSafeCollections.DelayedTask",
    }

    missing_classes = expected_classes - created_classes
    assert not missing_classes, (
        f"Missing thread-safe collections classes: {sorted(list(missing_classes))}"
    )
