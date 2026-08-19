class App {
    void leak() {
        String token = System.getenv("TOKEN");
        System.out.println(token);
    }

    void safe() {
        String fixed = "constant";
        System.out.println(fixed);
    }
}
