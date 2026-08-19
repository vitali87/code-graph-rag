void leak() {
  var token = Platform.environment['TOKEN'];
  print(token);
}

String safe() {
  return "constant";
}
