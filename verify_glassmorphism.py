from playwright.sync_api import sync_playwright

def verify_images():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        # Block external requests to speed up loading and avoid timeouts
        page.route("**/*", lambda route: route.continue_() if "localhost" in route.request.url else route.abort())

        try:
            # Go to the local server
            page.goto("http://localhost:8000/glassmorphism.html", wait_until="domcontentloaded", timeout=10000)

            # Wait a bit for images to render (even if domcontentloaded is fired)
            page.wait_for_timeout(2000)

            # Wait for the logo to be present
            logo = page.locator(".image-container img")
            logo.wait_for(state="visible", timeout=5000)

            # Verify logo source and loading
            src = logo.get_attribute("src")
            print(f"Logo src: {src}")

            is_loaded = page.evaluate("""
                (img) => img.complete && img.naturalHeight !== 0
            """, logo.element_handle())

            if is_loaded:
                print("Logo loaded successfully.")
            else:
                print("Logo failed to load.")

            # Verify background image
            # Note: Computed style might give the full URL or relative, let's check computed style
            computed_bg = page.evaluate("window.getComputedStyle(document.body).backgroundImage")
            print(f"Body background-image: {computed_bg}")

            if "background-image.png" in computed_bg:
                 print("Background image path looks correct.")
            else:
                 print("Background image path might be incorrect.")

            # Take a screenshot
            page.screenshot(path="verification_glassmorphism.png")
            print("Screenshot saved to verification_glassmorphism.png")

        except Exception as e:
            print(f"An error occurred: {e}")
        finally:
            browser.close()

if __name__ == "__main__":
    verify_images()
