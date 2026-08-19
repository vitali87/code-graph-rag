class App {
    void leak() {
        String token = System.getenv("TOKEN");
        System.out.println(token);
    }

    String safe() {
        return "constant";
    }
}
