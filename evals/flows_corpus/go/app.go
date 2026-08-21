package main

import (
	"fmt"
	"os"
)

func leak() {
	token := os.Getenv("TOKEN")
	fmt.Println(token)
}

func safe() {
	fixed := "constant"
	fmt.Println(fixed)
}
