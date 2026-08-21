object App {
  def leak(): Unit = {
    val token = System.getenv("TOKEN")
    println(token)
  }

  def safe(): Unit = {
    val fixed = "constant"
    println(fixed)
  }
}
