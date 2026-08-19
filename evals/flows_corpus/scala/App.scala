object App {
  def leak(): Unit = {
    val token = System.getenv("TOKEN")
    println(token)
  }

  def safe(): String = "constant"
}
