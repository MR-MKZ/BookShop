(function ($) {
  "use strict"

  /* 1. sticky And Scroll UP — rAF throttle to avoid scroll jank */
  var stickyTicking = false;
  function updateStickyHeader() {
    var scroll = $(window).scrollTop();
    if (scroll < 400) {
      $(".header-sticky").removeClass("sticky-bar");
      $('#back-top').stop(true, true).fadeOut(200);
    } else {
      $(".header-sticky").addClass("sticky-bar");
      $('#back-top').stop(true, true).fadeIn(200);
    }
    stickyTicking = false;
  }
  $(window).on('scroll', function () {
    if (!stickyTicking) {
      stickyTicking = true;
      window.requestAnimationFrame(updateStickyHeader);
    }
  });

  // Scroll Up
  $('#back-top a').on("click", function () {
    $('body,html').animate({
      scrollTop: 0
    }, 400);
    return false;
  });

  /* 2. slick Nav */
  // mobile_menu
  var menu = $('ul#navigation');
  if (menu.length) {
    menu.slicknav({
      prependTo: ".mobile_menu",
      closedSymbol: '+',
      openedSymbol: '-'
    });
  };


  /* 3. MainSlider-1 */
  // h1-hero-active
  function doDataAnimations(elements) {
    var animationEndEvents = 'webkitAnimationEnd mozAnimationEnd MSAnimationEnd oanimationend animationend';
    elements.each(function () {
      var $this = $(this);
      var $animationDelay = $this.data('delay') || '0s';
      var $animationType = 'animated ' + $this.data('animation');
      $this.css({
        'animation-delay': $animationDelay,
        '-webkit-animation-delay': $animationDelay
      });
      $this.addClass($animationType).one(animationEndEvents, function () {
        $this.removeClass($animationType);
        $this.css({ opacity: 1, visibility: 'visible' });
      });
    });
  }

  function mainSlider() {
    var BasicSlider = $('.slider-active');
    if (!BasicSlider.length || typeof BasicSlider.slick !== 'function') return;
    if (BasicSlider.hasClass('slick-initialized')) return;

    var slideCount = BasicSlider.children('.single-slider').length;
    if (!slideCount) return;

    var autoplaySpeed = parseInt(BasicSlider.attr('data-autoplay-speed'), 10);
    if (isNaN(autoplaySpeed) || autoplaySpeed < 1000) autoplaySpeed = 10000;

    BasicSlider.on('init', function () {
      BasicSlider.addClass('is-ready');
    });

    BasicSlider.slick({
      autoplay: slideCount > 1,
      autoplaySpeed: autoplaySpeed,
      dots: slideCount > 1,
      fade: true,
      cssEase: 'linear',
      speed: 700,
      arrows: false,
      infinite: slideCount > 1,
      waitForAnimate: false,
      adaptiveHeight: false,
      prevArrow: '<button type="button" class="slick-prev"><i class="ti-angle-left"></i></button>',
      nextArrow: '<button type="button" class="slick-next"><i class="ti-angle-right"></i></button>',
      responsive: [{
          breakpoint: 1024,
          settings: {
            slidesToShow: 1,
            slidesToScroll: 1,
            infinite: slideCount > 1
          }
        },
        {
          breakpoint: 991,
          settings: {
            slidesToShow: 1,
            slidesToScroll: 1,
            arrows: false
          }
        },
        {
          breakpoint: 767,
          settings: {
            slidesToShow: 1,
            slidesToScroll: 1,
            arrows: false,
            dots: slideCount > 1
          }
        }
      ]
    });
  }
  mainSlider();

  // Hero search overlay animations (outside slick slides)
  doDataAnimations($('.hero-search-overlay').find('[data-animation]'));


  // 4. selling-active
  $('.selling-active').slick({
    dots: false,
    infinite: true,
    autoplay: true,
    speed: 400,
    arrows: true,
    prevArrow: '<button type="button" class="slick-prev"><i class="fas fa-chevron-left"></i></button>',
    nextArrow: '<button type="button" class="slick-next"><i class="fas fa-chevron-right"></i></button>',
    slidesToShow: 6,
    slidesToScroll: 1,
    rtl: true,
    responsive: [{
        breakpoint: 1400,
        settings: {
          slidesToShow: 5,
          slidesToScroll: 1,
          infinite: true,
          dots: false,
        }
      },
      {
        breakpoint: 1200,
        settings: {
          slidesToShow: 4,
          slidesToScroll: 1,
          infinite: true,
          dots: false,
        }
      },
      {
        breakpoint: 992,
        settings: {
          slidesToShow: 3,
          slidesToScroll: 1,
          infinite: true,
          dots: false,
        }
      },
      {
        breakpoint: 768,
        settings: {
          slidesToShow: 2,
          slidesToScroll: 1,
          arrows: false
        }
      },
      {
        breakpoint: 480,
        settings: {
          slidesToShow: 2,
          slidesToScroll: 1,
          arrows: false
        }
      },
      {
        breakpoint: 380,
        settings: {
          slidesToShow: 1,
          slidesToScroll: 1,
          arrows: false
        }
      },
    ]
  });

  // 5. Single Img slider
  $('.services-active').slick({
    dots: false,
    infinite: true,
    autoplay: false,
    speed: 400,
    arrows: true,
    prevArrow: '<button type="button" class="slick-prev"><i class="fas fa-chevron-left"></i></button>',
    nextArrow: '<button type="button" class="slick-next"><i class="fas fa-chevron-right"></i></button>',
    slidesToShow: 1,
    slidesToScroll: 1,
    rtl: true,
    responsive: [{
        breakpoint: 1024,
        settings: {
          slidesToShow: 1,
          slidesToScroll: 1,
          infinite: true,
          dots: false,
        }
      },
      {
        breakpoint: 992,
        settings: {
          slidesToShow: 1,
          slidesToScroll: 1,
          infinite: true,
          dots: false,
        }
      },
      {
        breakpoint: 768,
        settings: {
          slidesToShow: 1,
          slidesToScroll: 1,
          arrows: false
        }
      },
      {
        breakpoint: 480,
        settings: {
          slidesToShow: 1,
          slidesToScroll: 1,
          arrows: false
        }
      },
    ]
  });

  /* 6. Nice Selectorp  — skip pagination page jumpers */
  var nice_Select = $('select').not('.page-select');
  if (nice_Select.length) {
    nice_Select.niceSelect();
  }
  /* Ensure pagination selects stay native (destroy accidental wrappers) */
  $('select.page-select').each(function () {
    var $sel = $(this);
    if ($sel.next('.nice-select').length && typeof $sel.niceSelect === 'function') {
      $sel.niceSelect('destroy');
    }
    $sel.css('display', '');
  });

  /* 7. data-background */
  $("[data-background]").each(function () {
    $(this).css("background-image", "url(" + $(this).attr("data-background") + ")")
  });

  /* 10. WOW active — skip on mobile / reduce scroll cost */
  if (typeof WOW !== 'undefined' && window.matchMedia('(min-width: 768px)').matches) {
    new WOW({ mobile: false, live: false }).init();
  }

  // 11. ---- Mailchimp js --------//  
  function mailChimp() {
    var $form = $('#mc_embed_signup').find('form');
    if ($form.length && typeof $.fn.ajaxChimp === 'function') {
      $form.ajaxChimp();
    }
  }
  mailChimp();


  // 12 Pop Up Img
  var popUp = $('.single_gallery_part, .img-pop-up');
  if (popUp.length && typeof $.fn.magnificPopup === 'function') {
    popUp.magnificPopup({
      type: 'image',
      gallery: {
        enabled: true
      }
    });
  }

  // 13 Pop Up Video
  var popUpVideo = $('.popup-video');
  if (popUpVideo.length && typeof $.fn.magnificPopup === 'function') {
    popUpVideo.magnificPopup({
      type: 'iframe'
    });
  }

  /* 14. counterUp*/
  if ($('.counter').length && typeof $.fn.counterUp === 'function') {
    $('.counter').counterUp({
      delay: 10,
      time: 3000
    });
  }


  //15. click counter Number js
  (function () {
    window.inputNumber = function (el) {
      var min = el.attr('min') || false;
      var max = el.attr('max') || false;
      var els = {};
      els.dec = el.prev();
      els.inc = el.next();

      el.each(function () {
        init($(this));
      });

      function init(el) {

        els.dec.on('click', decrement);
        els.inc.on('click', increment);

        function decrement() {
          var value = el[0].value;
          value--;
          if (!min || value >= min) {
            el[0].value = value;
          }
        }

        function increment() {
          var value = el[0].value;
          value++;
          if (!max || value <= max) {
            el[0].value = value++;
          }
        }
      }
    }
  })();
  inputNumber($('.input-number'));
  inputNumber($('.input-number2'));


})(jQuery);

// 16. categories-active
$('.categories-active').slick({
  dots: false,
  infinite: true,
  autoplay: true,
  speed: 400,
  arrows: true,
  prevArrow: '<button type="button" class="slick-prev"><i class="fas fa-chevron-left"></i></button>',
  nextArrow: '<button type="button" class="slick-next"><i class="fas fa-chevron-right"></i></button>',
  slidesToShow: 6,
  slidesToScroll: 1,
  rtl: true,
  responsive: [{
      breakpoint: 1400,
      settings: {
        slidesToShow: 5,
        slidesToScroll: 1,
        infinite: true,
        dots: false,
      }
    },
    {
      breakpoint: 1200,
      settings: {
        slidesToShow: 4,
        slidesToScroll: 1,
        infinite: true,
        dots: false,
      }
    },
    {
      breakpoint: 992,
      settings: {
        slidesToShow: 4,
        slidesToScroll: 1,
        infinite: true,
        dots: false,
      }
    },
    {
      breakpoint: 768,
      settings: {
        slidesToShow: 3,
        slidesToScroll: 1,
        arrows: false
      }
    },
    {
      breakpoint: 576,
      settings: {
        slidesToShow: 2,
        slidesToScroll: 1,
        arrows: false
      }
    },
    {
      breakpoint: 490,
      settings: {
        slidesToShow: 1,
        slidesToScroll: 1,
        arrows: false
      }
    },
  ]
});

// 17.testimonials
jQuery(document).ready(function ($) {
  "use strict";
  //  TESTIMONIALS CAROUSEL HOOK
  $("#customers-testimonials").owlCarousel({
    loop: true,
    center: true,
    items: 3,
    margin: 0,
    autoplay: true,
    dots: true,
    autoplayTimeout: 8500,
    smartSpeed: 450,
    responsive: {
      0: {
        items: 1
      },
      768: {
        items: 2
      },
      1170: {
        items: 3
      }
    }
  });
});

// 18. author-books
$('.author-books-box').slick({
  dots: true,
  infinite: true,
  autoplay: true,
  speed: 400,
  arrows: false,
  prevArrow: '<button type="button" class="slick-prev"><i class="fas fa-chevron-left"></i></button>',
  nextArrow: '<button type="button" class="slick-next"><i class="fas fa-chevron-right"></i></button>',
  slidesToShow: 5,
  slidesToScroll: 1,
  rtl: true,
  responsive: [{
      breakpoint: 1400,
      settings: {
        slidesToShow: 5,
        slidesToScroll: 1,
        infinite: true,
        dots: true,
      }
    },
    {
      breakpoint: 1200,
      settings: {
        slidesToShow: 4,
        slidesToScroll: 1,
        infinite: true,
        dots: true,
      }
    },
    {
      breakpoint: 992,
      settings: {
        slidesToShow: 3,
        slidesToScroll: 1,
        infinite: true,
        dots: true,
      }
    },
    {
      breakpoint: 768,
      settings: {
        slidesToShow: 2,
        slidesToScroll: 1,
        arrows: false,
        dots: true
      }
    },
    {
      breakpoint: 481,
      settings: {
        slidesToShow: 1,
        slidesToScroll: 1,
        arrows: false,
        dots: true
      }
    },
    {
      breakpoint: 380,
      settings: {
        slidesToShow: 1,
        slidesToScroll: 1,
        arrows: false,
        dots: true
      }
    },
  ]
});

// 19. Related-books
$('.related-books-box').slick({
  dots: true,
  infinite: true,
  autoplay: true,
  speed: 400,
  arrows: false,
  prevArrow: '<button type="button" class="slick-prev"><i class="fas fa-chevron-left"></i></button>',
  nextArrow: '<button type="button" class="slick-next"><i class="fas fa-chevron-right"></i></button>',
  slidesToShow: 5,
  slidesToScroll: 1,
  rtl: true,
  responsive: [{
      breakpoint: 1400,
      settings: {
        slidesToShow: 5,
        slidesToScroll: 1,
        infinite: true,
        dots: true,
      }
    },
    {
      breakpoint: 1200,
      settings: {
        slidesToShow: 4,
        slidesToScroll: 1,
        infinite: true,
        dots: true,
      }
    },
    {
      breakpoint: 992,
      settings: {
        slidesToShow: 3,
        slidesToScroll: 1,
        infinite: true,
        dots: true,
      }
    },
    {
      breakpoint: 768,
      settings: {
        slidesToShow: 2,
        slidesToScroll: 1,
        arrows: false,
        dots: true
      }
    },
    {
      breakpoint: 481,
      settings: {
        slidesToShow: 1,
        slidesToScroll: 1,
        arrows: false,
        dots: true
      }
    },
    {
      breakpoint: 380,
      settings: {
        slidesToShow: 1,
        slidesToScroll: 1,
        arrows: false,
        dots: true
      }
    },
  ]
});

// 20. Related-blogs
$('.related-blogs-box').slick({
  dots: true,
  infinite: true,
  autoplay: true,
  speed: 400,
  arrows: false,
  prevArrow: '<button type="button" class="slick-prev"><i class="fas fa-chevron-left"></i></button>',
  nextArrow: '<button type="button" class="slick-next"><i class="fas fa-chevron-right"></i></button>',
  slidesToShow: 2,
  slidesToScroll: 1,
  rtl: true,
  responsive: [{
      breakpoint: 1400,
      settings: {
        slidesToShow: 2,
        slidesToScroll: 1,
        infinite: true,
        dots: true,
      }
    },
    {
      breakpoint: 1200,
      settings: {
        slidesToShow: 2,
        slidesToScroll: 1,
        infinite: true,
        dots: true,
      }
    },
    {
      breakpoint: 992,
      settings: {
        slidesToShow: 2,
        slidesToScroll: 1,
        infinite: true,
        dots: true,
      }
    },
    {
      breakpoint: 768,
      settings: {
        slidesToShow: 1,
        slidesToScroll: 1,
        arrows: false,
        dots: true
      }
    },
    {
      breakpoint: 481,
      settings: {
        slidesToShow: 1,
        slidesToScroll: 1,
        arrows: false,
        dots: true
      }
    },
    {
      breakpoint: 380,
      settings: {
        slidesToShow: 1,
        slidesToScroll: 1,
        arrows: false,
        dots: true
      }
    },
  ]
});

// 21. author-books
$('.publisher-books-box').slick({
  dots: true,
  infinite: true,
  autoplay: true,
  speed: 400,
  arrows: false,
  prevArrow: '<button type="button" class="slick-prev"><i class="fas fa-chevron-left"></i></button>',
  nextArrow: '<button type="button" class="slick-next"><i class="fas fa-chevron-right"></i></button>',
  slidesToShow: 5,
  slidesToScroll: 1,
  rtl: true,
  responsive: [{
      breakpoint: 1400,
      settings: {
        slidesToShow: 5,
        slidesToScroll: 1,
        infinite: true,
        dots: true,
      }
    },
    {
      breakpoint: 1200,
      settings: {
        slidesToShow: 4,
        slidesToScroll: 1,
        infinite: true,
        dots: true,
      }
    },
    {
      breakpoint: 992,
      settings: {
        slidesToShow: 3,
        slidesToScroll: 1,
        infinite: true,
        dots: true,
      }
    },
    {
      breakpoint: 768,
      settings: {
        slidesToShow: 2,
        slidesToScroll: 1,
        arrows: false,
        dots: true
      }
    },
    {
      breakpoint: 481,
      settings: {
        slidesToShow: 1,
        slidesToScroll: 1,
        arrows: false,
        dots: true
      }
    },
    {
      breakpoint: 380,
      settings: {
        slidesToShow: 1,
        slidesToScroll: 1,
        arrows: false,
        dots: true
      }
    },
  ]
});

//range price (only on pages that have the filter widget)
(function () {
  const slider = document.getElementById("sliderPrice");
  if (!slider || typeof noUiSlider === "undefined") return;

  const rangeMin = parseInt(slider.dataset.min, 10);
  const rangeMax = parseInt(slider.dataset.max, 10);
  const step = parseInt(slider.dataset.step, 10);
  if (Number.isNaN(rangeMin) || Number.isNaN(rangeMax) || Number.isNaN(step)) return;

  const filterInputs = document.querySelectorAll("input.filter__input");

  noUiSlider.create(slider, {
    start: [rangeMin, rangeMax],
    connect: true,
    step: step,
    direction: "rtl",
    range: {
      min: rangeMin,
      max: rangeMax
    },
    format: {
      to: (value) => value,
      from: (value) => value
    }
  });

  slider.noUiSlider.on("update", (values, handle) => {
    if (filterInputs[handle]) filterInputs[handle].value = values[handle];
  });

  filterInputs.forEach((input, indexInput) => {
    input.addEventListener("change", () => {
      slider.noUiSlider.setHandle(indexInput, input.value);
    });
  });
})();
