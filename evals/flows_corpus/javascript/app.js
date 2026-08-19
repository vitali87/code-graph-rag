function leak() {
  const token = process.env.TOKEN;
  console.log(token);
}

function safe() {
  const fixed = "constant";
  console.log(fixed);
}
