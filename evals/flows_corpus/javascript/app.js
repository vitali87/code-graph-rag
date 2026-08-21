function leak() {
  const value = process.env.TOKEN;
  console.log(value);
}

function safe() {
  const fixed = "constant";
  console.log(fixed);
}
