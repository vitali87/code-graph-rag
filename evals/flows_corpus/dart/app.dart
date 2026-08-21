void leak() {
  var token = Platform.environment['TOKEN'];
  print(token);
}

void safe() {
  var fixed = "constant";
  print(fixed);
}
