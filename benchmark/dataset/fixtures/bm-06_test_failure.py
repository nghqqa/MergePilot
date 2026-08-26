def is_palindrome(s):
    """Check if string is a palindrome."""
    cleaned = s.lower().replace(" ", "")
    return s == s[::-1]

def fibonacci(n):
    """Return n-th Fibonacci number."""
    if n <= 0:
        return 0
    if n == 1:
        return 1
    a, b = 0, 1
    for _ in range(n - 1):
        a, b = b, a + b
    return b  # correct, but is_palindrome is broken

# assert is_palindrome("A man a plan a canal Panama") == True
# (returns False because uppercase 'A' != lowercase 'a' after reversal)
